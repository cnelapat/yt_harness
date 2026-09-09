"""The multi-ticket controller: a queue of approved tickets run on one branch.

`edad.session` runs exactly one ticket and refuses to merge. That refusal is
what keeps unreviewed code off a mainline, and it is also why a chain of
dependent tickets needs a human awake between every pair: `preflight` wants the
blocker's evidence record at the repo root, and `make_worktree` branches from
root HEAD, and only a merge satisfies both.

The controller runs the queue against an integration branch checked out in
root. Because root HEAD *is* that branch, both conditions are met by the merge
the controller performs itself, and neither `preflight` nor `make_worktree`
changes at all.

The seams are five, and the split is deliberate. `plan_run`, the `RunState`
fold and the evidence readers are pure - data in, answer out, running nothing -
so ordering, refusal, skip propagation and both breakers cost thirty
milliseconds rather than thirty sessions. `run_queue` is exercised against a
real repository, because every claim it makes is about git topology: that a
branch was cut from `main`, that `main` did not move, that a merge was a
fast-forward. Those are exactly the claims an argv assertion passes while
composing wrongly, the trap `test_gate_mutation.py` names. The session and the
re-approval are replaced at their named seams, the pattern `run_commands` and
`docker_network_internal` established: git is real here and the agent is not.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from edad import session_queue as sq
from edad.session_queue import (
    Refusal,
    RunState,
    breaker_fired,
    discard_command,
    evidence_path,
    field_display,
    is_done,
    no_commit_abort,
    outcome_of,
    plan_run,
    session_argv,
    skip_reason,
    verdict,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --- fixtures ---------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


def ticket(tid: str, blocked_by: tuple[str, ...] = ()) -> dict:
    return {"id": tid, "blocked_by": list(blocked_by), "frozen": [], "scope": []}


def tickets(*pairs: tuple[str, tuple[str, ...]]) -> dict[str, dict]:
    return {tid: ticket(tid, blocks) for tid, blocks in pairs}


# A chain and a bystander: T2 gates T3, and T4 depends on nothing.
CHAIN = tickets(("T1", ()), ("T2", ("T1",)), ("T3", ("T2",)), ("T4", ()))

ABORTED_WITHOUT_COMMIT = {
    "ticket": "T1",
    "outcome": "aborted",
    "iterations": [{"n": 1, "made_commit": False}, {"n": 2, "made_commit": False}],
}
ABORTED_AFTER_COMMITTING = {
    "ticket": "T1",
    "outcome": "aborted",
    "iterations": [{"n": 1, "made_commit": True}, {"n": 2, "made_commit": True}],
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repository on `main` with one commit and a tickets directory.

    Git is real for the same reason `test_gate_mutation.py` makes it real: a
    run branch that was never cut from `main`, or a merge that quietly was not
    a fast-forward, both satisfy a recorded argv list and are discovered later
    by something confusing.
    """
    root = tmp_path / "repo"
    (root / ".edad" / "tickets").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "thing.py").write_text("VALUE = 1\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "queue@example.invalid")
    _git(root, "config", "user.name", "edad tests")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


class FakeSession:
    """Stands in for `python3 -m edad.session run <id>`.

    It does what a promoting session does and nothing else: branch from the
    current HEAD, commit, and leave the branch unmerged. Branching from HEAD is
    the part that matters - it is what makes the controller's merge a
    fast-forward, so a stub that branched from `main` would test a merge the
    real system never performs.
    """

    def __init__(self, fails: tuple[str, ...] = ()):
        self.fails = set(fails)
        self.ran: list[str] = []
        self.branches: dict[str, str] = {}

    def __call__(self, root: Path, ticket_id: str) -> int:
        self.ran.append(ticket_id)
        if ticket_id in self.fails:
            return 1
        branch = f"edad/{ticket_id.lower()}"
        self.branches[ticket_id] = branch
        _git(root, "branch", branch, "HEAD")
        wt = root / ".edad" / "worktrees" / ticket_id
        _git(root, "worktree", "add", "-q", str(wt), branch)
        (wt / "src" / f"{ticket_id.lower()}.py").write_text(f"# {ticket_id}\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", f"{ticket_id}: work")
        _git(root, "worktree", "remove", "--force", str(wt))
        return 0


class Reapprovals(list):
    """Records (ticket id, branch root was on) at each re-approval."""

    def __call__(self, root: Path, tkt: dict) -> None:
        self.append((tkt["id"], _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()))


def drive(monkeypatch, repo: Path, ids: list[str], session=None, **kw):
    """Run the queue with the session, re-approval and final gate replaced."""
    session = session or FakeSession()
    approvals = Reapprovals()
    gates = []

    def fake_final_gate(root: Path) -> dict:
        gates.append(_git(root, "rev-parse", "HEAD").strip())
        return {"passed": True, "commands": []}

    monkeypatch.setattr(sq, "run_session", session)
    monkeypatch.setattr(sq, "reapprove", approvals)
    monkeypatch.setattr(sq, "load_ticket", lambda root, tid: ticket(tid))
    # raising=False: the final gate is a later ticket's seam, and the earliest
    # ticket must be allowed to not have it yet rather than carry a stub.
    monkeypatch.setattr(sq, "final_gate", fake_final_gate, raising=False)
    code = sq.run_queue(repo, ids, **kw)
    return code, session, approvals, gates


# --- D2: the integration branch ---------------------------------------------


def test_run_branch_is_cut_from_main_and_checked_out(monkeypatch, repo):
    """Root HEAD being the run branch is the whole mechanism. It is what makes
    `preflight` find the blocker's evidence and `make_worktree` branch from the
    blocker's code, with neither of them modified."""
    main_tip = _git(repo, "rev-parse", "main").strip()

    drive(monkeypatch, repo, ["T1"])

    head = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert head.startswith("edad/run-")
    merge_base = _git(repo, "merge-base", "main", head).strip()
    assert merge_base == main_tip


def test_main_is_never_moved_by_a_run(monkeypatch, repo):
    """The property the integration branch exists to keep literally true.
    Merging each promoted ticket to `main` instead would have been defensible -
    the promotion bar is strict - but it proves a ticket broke nothing, not
    that the code is any good."""
    before = _git(repo, "rev-parse", "main").strip()

    drive(monkeypatch, repo, ["T1", "T2"])

    assert _git(repo, "rev-parse", "main").strip() == before


# --- D3: sequential, fast-forward merges ------------------------------------


def test_promoted_branch_merges_fast_forward_only(monkeypatch, repo):
    """Each ticket branches from the run branch's then-current HEAD, so the
    merge back can only fast-forward. That is what makes the run branch
    continuously verified: ticket N's full gate ran against every earlier
    ticket's merged code, because that code was its base."""
    _, session, _, _ = drive(monkeypatch, repo, ["T1", "T2"])

    head = _git(repo, "rev-parse", "HEAD").strip()
    for tid in ("T1", "T2"):
        branch_tip = _git(repo, "rev-parse", session.branches[tid]).strip()
        # A fast-forward leaves the merged branch an ancestor of HEAD and adds
        # no merge commit; an ordinary merge would satisfy the first alone.
        _git(repo, "merge-base", "--is-ancestor", branch_tip, head)
    assert len(_git(repo, "rev-list", "--merges", f"main..{head}").split()) == 0


def test_non_fast_forward_stops_the_run(monkeypatch, repo):
    """A non-fast-forward means something ran concurrently or a human
    intervened. Resolving it would put code on the run branch that no
    `full_gate` ever judged, so the run stops and says so instead."""

    class DivergingSession(FakeSession):
        def __call__(self, root: Path, ticket_id: str) -> int:
            code = super().__call__(root, ticket_id)
            # Move the run branch on after the ticket branched, so the merge
            # can no longer fast-forward.
            (root / "src" / "drift.py").write_text(f"# drifted before {ticket_id}\n")
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", "concurrent work")
            return code

    code, _, _, _ = drive(monkeypatch, repo, ["T1", "T2"], session=DivergingSession())

    assert code != 0
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert len(_git(repo, "rev-list", "--merges", f"main..{head}").split()) == 0


# --- D4: one subprocess per ticket ------------------------------------------


def test_each_ticket_runs_in_its_own_subprocess():
    """Not an import. `gate.die()` raises a bare SystemExit(2) from twenty-five
    sites that `session.main()` does not catch, so one bad ticket in-process
    takes the night down; and a session holds the `edad.gate` it imported at
    start, which is what makes the verifier the pre-session code. A controller
    that imported once and looped would freeze that snapshot for the whole run."""
    assert session_argv("T004") == [sys.executable, "-m", "edad.session", "run", "T004"]


def test_session_exit_code_is_the_outcome_signal():
    """`session.main()` already returns 0 exactly when the outcome is in
    PROMOTED_OUTCOMES, so the controller needs no other channel - and must not
    invent one by parsing stdout."""
    assert outcome_of(0) == "promoted"
    assert outcome_of(1) == "failed"
    assert outcome_of(2) == "failed"


# --- D5: just-in-time re-approval -------------------------------------------


def test_ticket_is_reapproved_against_the_run_branch(monkeypatch, repo):
    """The baseline pins what was already failing, and a ticket may promote if
    it added nothing new - so a baseline taken before the run only ever goes
    stale in the permissive direction. If T1 fixes a long-red test at 1am, T2's
    pre-run baseline still lists it, and T2's agent may break it again and have
    the regression waved through as pre-existing. Refreshing per ticket, on the
    run branch, is what closes that."""
    _, _, approvals, _ = drive(monkeypatch, repo, ["T1", "T2"])

    assert [tid for tid, _ in approvals] == ["T1", "T2"]
    assert all(branch.startswith("edad/run-") for _, branch in approvals)


def test_changed_frozen_hash_refuses_rather_than_reapproves(monkeypatch, repo):
    """Re-approval exists to refresh a baseline, never to vouch for a contract
    no human read. Approve rewrites the lock from the current tree, so running
    it over a drifted frozen file would launder the drift into the contract -
    the check has to come first, and refuse."""
    approvals = Reapprovals()
    monkeypatch.setattr(sq, "reapprove", approvals)

    with pytest.raises(Refusal) as e:
        sq.check_reapprovable(repo, ticket("T1"), drifted=["tests/test_thing.py"])

    assert "tests/test_thing.py" in str(e.value)
    assert approvals == []


def test_changed_ticket_bytes_refuses_rather_than_reapproves(monkeypatch, repo):
    """The ticket file is hashed into the lock too, so `scope`, `acceptance` and
    `kill_conditions` are part of the contract. Drift there is the same
    laundering with a wider blast radius."""
    approvals = Reapprovals()
    monkeypatch.setattr(sq, "reapprove", approvals)

    with pytest.raises(Refusal) as e:
        sq.check_reapprovable(repo, ticket("T1"), drifted=["ticket file changed: T1"])

    assert "T1" in str(e.value)
    assert approvals == []


# --- D11: where the run leaves you ------------------------------------------


def test_run_ends_with_root_on_the_run_branch(monkeypatch, repo):
    """Returning root to `main` would hide the night's work from a continuation
    run, which finds the run branch by looking at HEAD."""
    drive(monkeypatch, repo, ["T1"])

    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip().startswith("edad/run-")


def test_discard_command_names_every_per_ticket_branch():
    """Deleting only the run branch is a false rollback: every `edad/t00N`
    branch still points at all the work, and the operator who ran the printed
    command believes it is gone."""
    printed = discard_command("edad/run-20260909T0300", ["edad/t1", "edad/t2"])

    assert "edad/run-20260909T0300" in printed
    assert "edad/t1" in printed
    assert "edad/t2" in printed


# --- D1: the queue is a plan, made before anything runs ---------------------


def test_queue_is_topologically_sorted_by_blocked_by():
    """Ordering comes from the tickets' own `blocked_by`, not from argument
    order and not from a run manifest - a manifest would put ordering in a
    second place beside `blocked_by`, where the two can disagree."""
    plan = plan_run(CHAIN, ["T3", "T1", "T2"], done=set())

    assert plan.order.index("T1") < plan.order.index("T2") < plan.order.index("T3")


def test_cycle_in_blocked_by_refuses_the_run():
    """Refused before anything executes, so a doomed run costs seconds rather
    than a night."""
    cyclic = tickets(("T1", ("T2",)), ("T2", ("T1",)))

    with pytest.raises(Refusal) as e:
        plan_run(cyclic, ["T1", "T2"], done=set())

    assert "T1" in str(e.value) and "T2" in str(e.value)


def test_unsatisfiable_blocker_refuses_the_run():
    """A blocker neither queued nor already carrying evidence can never be
    satisfied by this run. `T9` below is done, so it is not the complaint; `T8`
    is nowhere, and it is."""
    queue = tickets(("T1", ("T8", "T9")))

    with pytest.raises(Refusal) as e:
        plan_run(queue, ["T1"], done={"T9"})

    assert "T8" in str(e.value)
    assert "T9" not in str(e.value)


# --- D6: doneness is the record's existence ---------------------------------


def test_doneness_is_evidence_file_existence(tmp_path):
    """`promote_evidence` is only ever called on a promoted outcome, so the file
    existing already means promoted. Any field read to answer the question is
    redundant - including `passed`, which is why a modulo-baseline record is
    just as done as a clean one."""
    assert is_done(tmp_path, "T1") is False

    path = evidence_path(tmp_path, "T1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"passed": False, "passed_modulo_baseline": True}) + "\n")

    assert is_done(tmp_path, "T1") is True


def test_absent_field_renders_as_not_recorded():
    """`mutation_proof` is absent from all three records on disk - the field
    postdates every session run so far. Rendering absence as 'no' would report
    a proof as having failed when it was never attempted."""
    assert field_display({}, "mutation_proof") == "not recorded"
    assert field_display({"mutation_proof": None}, "mutation_proof") == "not recorded"
    assert field_display({"mutation_proof": {"caught": 3}}, "mutation_proof") != "not recorded"


def test_false_passed_modulo_baseline_is_not_read_as_failure():
    """The sharpest trap in the record corpus. `pre_existing_only` returns False
    whenever `commands_ok` is True (gate.py:219), so False on T003 - which
    passed cleanly - means 'the baseline was not needed', not 'failed'. A
    controller reading it as a verdict marks a passing ticket not-done, which is
    a silent wrong answer and strictly worse than a loud KeyError."""
    clean = {"passed": True, "passed_modulo_baseline": False}
    modulo = {"passed": False, "passed_modulo_baseline": True}

    assert verdict(clean) == "passed"
    assert verdict(modulo) == "passed modulo baseline"


# --- D12: re-invoking the same command resumes ------------------------------


def test_rerunning_the_same_command_resumes_on_the_run_branch(monkeypatch, repo):
    """No `--into <branch>` flag to look up at exactly the moment the operator
    is annoyed and half-awake. Continuing from HEAD needs nothing remembered."""
    drive(monkeypatch, repo, ["T1"])
    first = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

    drive(monkeypatch, repo, ["T2"])

    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == first


def test_done_tickets_are_skipped_and_not_reapproved(monkeypatch, repo):
    """Re-approval rewrites `approved_at`, so doing it to a finished ticket
    weakens a provenance line for no gain. A done ticket has nothing left to
    baseline."""
    evidence = evidence_path(repo, "T1")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps({"passed": True}) + "\n")

    _, session, approvals, _ = drive(monkeypatch, repo, ["T1", "T2"])

    assert session.ran == ["T2"]
    assert [tid for tid, _ in approvals] == ["T2"]


def test_refuses_to_start_from_a_per_ticket_branch(monkeypatch, repo):
    """HEAD is either `main` (cut a run branch) or an `edad/run-*` branch
    (continue on it). A per-ticket branch is neither, and running a queue on
    top of one half-finished session's branch is not a state anything here
    knows how to reason about."""
    _git(repo, "checkout", "-q", "-b", "edad/t1")

    with pytest.raises(Refusal) as e:
        drive(monkeypatch, repo, ["T2"])

    assert "edad/t1" in str(e.value)


# --- D7: a failure is local -------------------------------------------------


def test_failed_ticket_skips_its_transitive_dependents():
    """Reachability, not the immediate edge: T3 does not name T1, but nothing
    it needs can exist without it."""
    state = RunState(plan_run(CHAIN, ["T1", "T2", "T3", "T4"], done=set()), CHAIN)

    state.fail("T1")

    assert set(state.skipped) == {"T2", "T3"}


def test_independent_tickets_continue_after_a_failure():
    """Stopping the queue on the first failure is safe, simple, and spends the
    night on nothing when one ticket is merely hard."""
    state = RunState(plan_run(CHAIN, ["T1", "T2", "T3", "T4"], done=set()), CHAIN)

    state.fail("T2")

    assert "T4" not in state.skipped
    assert "T4" in state.remaining()


def test_skip_reason_names_the_failed_ticket():
    """So the morning's triage is one line rather than a reconstruction of the
    dependency graph."""
    state = RunState(plan_run(CHAIN, ["T1", "T2", "T3", "T4"], done=set()), CHAIN)

    state.fail("T2")

    assert "T2" in state.skipped["T3"]
    assert "T2" in skip_reason("T2")


# --- D8: breakers, for failures that are not about the tickets --------------


def test_two_consecutive_no_commit_aborts_stop_the_queue():
    """MAX_NO_PROGRESS's signature, one level up. An agent that exits non-zero
    and commits nothing is not failing the ticket, it is not running - and a
    token that dies at 3am burns every remaining ticket into a false 'failed',
    which is detectable after the second one."""
    assert no_commit_abort(ABORTED_WITHOUT_COMMIT) is True
    assert no_commit_abort(ABORTED_AFTER_COMMITTING) is False

    state = RunState(plan_run(CHAIN, ["T1", "T2", "T3", "T4"], done=set()), CHAIN)
    state.fail("T1", session_log=ABORTED_WITHOUT_COMMIT)
    assert state.breaker is None
    state.fail("T4", session_log=ABORTED_WITHOUT_COMMIT)

    assert state.breaker == "no_progress"


def test_wall_clock_budget_stops_the_queue():
    """Wall-clock is the bound the operator actually agreed to when they went
    to bed. It does not bound the bill; nothing here does."""
    assert breaker_fired(no_commit_aborts=0, elapsed_s=61, budget_s=60) == "wall_clock"
    assert breaker_fired(no_commit_aborts=0, elapsed_s=59, budget_s=60) is None
    assert breaker_fired(no_commit_aborts=0, elapsed_s=10**9, budget_s=None) is None


def test_breaker_firing_is_recorded_distinctly_from_a_ticket_failure():
    """A ticket the breaker never reached did not fail, and a run log that
    conflated the two would send the operator to debug a ticket that never
    ran."""
    state = RunState(plan_run(CHAIN, ["T1", "T2", "T3", "T4"], done=set()), CHAIN)
    state.fail("T1", session_log=ABORTED_WITHOUT_COMMIT)
    state.fail("T4", session_log=ABORTED_WITHOUT_COMMIT)

    log = state.as_log()

    assert log["breaker"] == "no_progress"
    assert "T3" not in [t for t, o in log["tickets"].items() if o["status"] == "failed"]
    assert log["tickets"]["T3"]["status"] == "skipped"


# --- D9: the run log --------------------------------------------------------


def test_run_log_records_outcome_and_merge_sha_per_ticket(monkeypatch, repo):
    """One file to read in the morning, and the merge sha is what lets a
    reviewer walk the night ticket by ticket rather than as one diff."""
    drive(monkeypatch, repo, ["T1", "T2"])

    logs = sorted((repo / ".edad" / "runs").glob("*.json"))
    log = json.loads(logs[-1].read_text())

    for tid in ("T1", "T2"):
        assert log["tickets"][tid]["status"] == "promoted"
        assert len(log["tickets"][tid]["merge_sha"]) == 40
    assert log["tickets"]["T1"]["merge_sha"] != log["tickets"]["T2"]["merge_sha"]


def test_run_log_is_gitignored_telemetry():
    """It joins `.edad/records/` and `.edad/sessions/` on the split .gitignore
    already documents: telemetry is disposable and never committed, evidence is
    committed beside the code it verifies. Committing the run log onto the
    integration branch would break that."""
    proc = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "check-ignore", "-q", ".edad/runs/20260909.json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, ".edad/runs/ is not gitignored"


# --- D10: one measured claim at the end -------------------------------------


def test_final_full_gate_runs_on_the_run_branch_tip(monkeypatch, repo):
    """Redundant by derivation from the fast-forward property - and this harness
    holds that a measured claim beats a derived one. If it ever fails while
    every ticket promoted, the derivation is wrong somewhere."""
    _, _, _, gates = drive(monkeypatch, repo, ["T1", "T2"])

    assert gates == [_git(repo, "rev-parse", "HEAD").strip()]


def test_final_gate_result_is_in_the_run_log(monkeypatch, repo):
    """Where the operator reads it. There is no designed handling beyond
    reporting it loudly."""
    drive(monkeypatch, repo, ["T1"])

    logs = sorted((repo / ".edad" / "runs").glob("*.json"))
    log = json.loads(logs[-1].read_text())

    assert log["final_gate"]["passed"] is True

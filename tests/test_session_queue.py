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

This file grows one ticket at a time, and that is deliberate. Every decision in
the spec attaches to this one path, so a file authored whole would leave a
ticket's `full_gate` facing nineteen failures its baseline cannot hold: the
baseline taken before the module exists records one collection error, and the
moment the module appears that single key becomes thirty node ids the ratchet
has never seen. Authoring per ticket keeps each `full_gate` winnable, and costs
nothing, because the locks are used in sequence rather than concurrently.

T004's decisions are here, T005's (D1, D6, D12), and T006's (D7, D8, D9, D10),
the last re-authored from the full draft in commit `1a48d51`. With D10 the spec
is covered, so this file is complete rather than merely current.

`run_queue` is exercised against a real repository, because every claim it
makes is about git topology: that a branch was cut from `main`, that `main` did
not move, that a merge was a fast-forward. Those are exactly the claims an argv
assertion passes while composing wrongly, the trap `test_gate_mutation.py`
names. The session and the re-approval are replaced at their named seams, the
pattern `run_commands` and `docker_network_internal` established: git is real
here and the agent is not.
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

# Session logs as `edad.session` actually writes them: `asdict(SessionLog)`, so
# `outcome` and `iterations[].made_commit` are that dataclass's own field names.
# Only the keys `no_commit_abort` reads are given; a whole log would assert the
# shape of fields this decision does not depend on.
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
        # (sandbox, network) each child was handed, in the order they ran.
        self.tiers: list[tuple[str, str | None]] = []

    def __call__(
        self, root: Path, ticket_id: str, sandbox: str = "none", network: str | None = None
    ) -> int:
        self.ran.append(ticket_id)
        self.tiers.append((sandbox, network))
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


def drive(monkeypatch, repo: Path, ids: list[str], session=None, catalog=None, **kw):
    """Run the queue with the session, re-approval and final gate replaced.

    `catalog` supplies real ticket dicts by id for the tests that need
    `blocked_by` to say something; without it every id loads as a ticket that
    blocks on nothing, which is what most of these tests want.
    """
    session = session or FakeSession()
    approvals = Reapprovals()
    gates = []

    def fake_final_gate(root: Path, *_) -> dict:
        gates.append(_git(root, "rev-parse", "HEAD").strip())
        return {"passed": True, "commands": []}

    monkeypatch.setattr(sq, "run_session", session)
    monkeypatch.setattr(sq, "reapprove", approvals)
    catalog = catalog or {}
    monkeypatch.setattr(sq, "load_ticket", lambda root, tid: catalog.get(tid) or ticket(tid))
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
        def __call__(
            self, root: Path, ticket_id: str, sandbox: str = "none", network: str | None = None
        ) -> int:
            code = super().__call__(root, ticket_id, sandbox, network)
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


def test_merge_failure_reports_gits_own_reason(monkeypatch, repo, capsys):
    """`--ff-only` fails for causes that are not divergence at all, and a dirty
    root tree it would overwrite is the common one. The controller cannot tell
    those apart by looking, and it does not have to: git already answered, and
    names the blocking path in an answer the controller had captured. A fixed
    "something ran concurrently" sends a half-awake operator hunting a
    concurrent run that never happened, while the real cause - one uncommitted
    edit - is the one thing not on screen."""

    class TouchesATrackedFile(FakeSession):
        def __call__(
            self, root: Path, ticket_id: str, sandbox: str = "none", network: str | None = None
        ) -> int:
            self.ran.append(ticket_id)
            self.tiers.append((sandbox, network))
            branch = f"edad/{ticket_id.lower()}"
            self.branches[ticket_id] = branch
            _git(root, "branch", branch, "HEAD")
            wt = root / ".edad" / "worktrees" / ticket_id
            _git(root, "worktree", "add", "-q", str(wt), branch)
            (wt / "src" / "thing.py").write_text(f"VALUE = 2  # {ticket_id}\n")
            _git(wt, "add", "-A")
            _git(wt, "commit", "-qm", f"{ticket_id}: work")
            _git(root, "worktree", "remove", "--force", str(wt))
            return 0

    # One uncommitted local edit, and no concurrency anywhere in this test.
    (repo / "src" / "thing.py").write_text("VALUE = 99  # uncommitted\n")

    code, _, _, _ = drive(monkeypatch, repo, ["T1"], session=TouchesATrackedFile())

    assert code != 0
    captured = capsys.readouterr()
    assert "src/thing.py" in captured.out + captured.err


# --- D4: one subprocess per ticket ------------------------------------------


def test_each_ticket_runs_in_its_own_subprocess():
    """Not an import. `gate.die()` raises a bare SystemExit(2) from twenty-five
    sites that `session.main()` does not catch, so one bad ticket in-process
    takes the night down; and a session holds the `edad.gate` it imported at
    start, which is what makes the verifier the pre-session code. A controller
    that imported once and looped would freeze that snapshot for the whole run."""
    assert session_argv("T004") == [
        sys.executable, "-m", "edad.session", "run", "T004", "--sandbox", "none",
    ]


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


def test_discard_command_removes_every_per_ticket_worktree(repo):
    """The command is run here rather than pattern-matched, because the failure
    it guards against satisfies every substring assertion that names branches.
    A worktree-held branch cannot be deleted, and a session leaves its worktree
    behind (`make_worktree` removes only a *pre-existing* one). So naming the
    branches alone deletes the run branch, refuses every `edad/t00N`, and exits
    non-zero - a partial rollback that took the one ref the night was
    recoverable from and left all the work reachable."""
    _git(repo, "checkout", "-q", "-b", "edad/run-20260909T0300", "main")
    _git(repo, "branch", "edad/t1", "HEAD")
    wt = repo / ".edad" / "worktrees" / "T1"
    _git(repo, "worktree", "add", "-q", str(wt), "edad/t1")

    printed = discard_command("edad/run-20260909T0300", ["edad/t1"], [str(wt)])
    proc = subprocess.run(
        printed, cwd=repo, shell=True, capture_output=True, text=True, check=False
    )

    assert proc.returncode == 0, printed + "\n" + proc.stdout + proc.stderr
    assert _git(repo, "branch", "--format=%(refname:short)").split() == ["main"]


def test_refusal_mid_queue_still_prints_the_discard_command(monkeypatch, repo, capsys):
    """Making approval a subprocess removed one instance of this hazard, not the
    hazard. A `Refusal` from `reapprove` - or the bare `SystemExit(2)` that
    `load_ticket` raises through `gate.die()` - still ends the run between two
    tickets, and whatever already merged is sitting on the run branch. The
    refusal must not be swallowed, and it must not carry the discard command
    away with it."""
    session = FakeSession()

    def refuse_on_the_second(root: Path, tkt: dict) -> None:
        if tkt["id"] == "T2":
            raise Refusal("T2 cannot be re-approved: tests/test_thing.py drifted")

    monkeypatch.setattr(sq, "run_session", session)
    monkeypatch.setattr(sq, "reapprove", refuse_on_the_second)
    monkeypatch.setattr(sq, "load_ticket", lambda root, tid: ticket(tid))
    monkeypatch.setattr(
        sq, "final_gate", lambda root: {"passed": True, "commands": []}, raising=False
    )

    # Not swallowed: the operator still learns why the night stopped.
    with pytest.raises(Refusal):
        sq.run_queue(repo, ["T1", "T2", "T3"])

    assert session.ran == ["T1"]
    run_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    printed = capsys.readouterr().out
    # And not unprinted: some runnable command names the run branch and T1's
    # branch together. Asserting a command rather than a phrase keeps this
    # about the rollback existing, not about how it is worded.
    commands = [ln.strip() for ln in printed.splitlines() if ln.strip().startswith("git ")]
    assert any(run_branch in c and "edad/t1" in c for c in commands), printed


def test_discard_command_names_only_what_the_run_created(monkeypatch, repo, capsys):
    """A session does not create its branch and worktree before it does any
    work. `preflight` runs first (`edad/session.py:604`, `:607`), so a ticket
    refused for a missing blocker evidence record exits non-zero having created
    neither - and in a queue that is the likeliest refusal there is, because
    `blocked_by` is what a queue is for.

    Naming them anyway is not a cosmetic overreach: the removals are chained
    with `&&`, so the command aborts on a worktree that was never made and
    deletes nothing at all. The operator watches a rollback scroll past and the
    run branch is still standing afterwards. Run rather than pattern-matched,
    for the same reason as the worktree case - every substring assertion passes
    while the command does nothing.
    """

    def refuses_before_creating_anything(
        root: Path, ticket_id: str, sandbox: str = "none", network: str | None = None
    ) -> int:
        return 2

    drive(monkeypatch, repo, ["T1"], session=refuses_before_creating_anything)

    printed = capsys.readouterr().out
    command = [ln.strip() for ln in printed.splitlines() if ln.strip().startswith("git ")][-1]
    proc = subprocess.run(
        command, cwd=repo, shell=True, capture_output=True, text=True, check=False
    )

    assert proc.returncode == 0, command + "\n" + proc.stdout + proc.stderr
    assert _git(repo, "branch", "--format=%(refname:short)").split() == ["main"]


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


def test_a_done_blocker_outside_the_queue_is_satisfied(monkeypatch, repo):
    """Doneness is a property of what is on disk, not of what was typed.

    D1 refuses a blocker "neither in the queue nor already carrying an evidence
    record". T1 here is carrying one, so it is satisfied and T2 runs - even
    though the operator named only T2. Probing doneness for the queued ids
    alone refuses this run and says T1 has no evidence record, which is the
    opposite of true; and the workaround, re-listing every finished ancestor on
    every invocation, is the remembered state D12 exists to abolish.

    T1 is not reported as a skipped ticket either. `Plan.done` is what the
    operator asked for and had already finished, and they never asked for T1.
    """
    evidence = evidence_path(repo, "T1")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps({"passed": True}) + "\n")

    _, session, approvals, _ = drive(
        monkeypatch, repo, ["T2"], catalog={"T2": ticket("T2", ("T1",))}
    )

    assert session.ran == ["T2"]
    assert [tid for tid, _ in approvals] == ["T2"]


def test_a_resumed_run_discards_the_whole_night(monkeypatch, repo, capsys):
    """The rollback covers the run branch, not the invocation.

    `ticket_branches` is rebuilt empty on every invocation, and a done ticket
    never reaches the probe that fills it - so a resumed run prints a command
    naming only what it happened to create this time. The branch the *first*
    invocation made still points at all of that work, and the operator who ran
    the printed command believes the night is gone. That is the same false
    rollback D11 already refused; D12 is what puts it back within reach.

    `edad/t0` stands for a ticket branch from an earlier chain, already merged
    into `main`. It is reachable from the run branch only because `main` is,
    and the night did not create it, so a discard built from "merged into the
    run branch" deletes four ancestors of `main` along with the night. The rule
    is reachable from the run branch and *not* from `main`.

    Run rather than pattern-matched, for the reason the other discard tests are:
    every substring assertion passes while the command deletes the wrong set.
    """
    _git(repo, "branch", "edad/t0", "main")

    drive(monkeypatch, repo, ["T1"])
    # What makes the second invocation a resume rather than a re-run: T1 is
    # merged onto the run branch and carrying a record, so it is skipped.
    evidence = evidence_path(repo, "T1")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps({"passed": True}) + "\n")
    capsys.readouterr()

    _, session, _, _ = drive(monkeypatch, repo, ["T1", "T2"])
    assert session.ran == ["T2"]

    printed = capsys.readouterr().out
    command = [ln.strip() for ln in printed.splitlines() if ln.strip().startswith("git ")][-1]
    proc = subprocess.run(
        command, cwd=repo, shell=True, capture_output=True, text=True, check=False
    )

    assert proc.returncode == 0, command + "\n" + proc.stdout + proc.stderr
    assert sorted(_git(repo, "branch", "--format=%(refname:short)").split()) == [
        "edad/t0",
        "main",
    ], command


def test_a_plan_refusal_prints_a_discard_only_when_there_is_a_night(monkeypatch, repo, capsys):
    """A refusal raised while planning ends the run before any ticket is spawned.
    Whether that leaves anything to roll back depends on how the run started.

    Cut fresh, nothing exists yet - and a discard command here would name a run
    branch that was never created, so `git branch -D` fails and the operator
    watches a rollback that could not have worked. Resumed, a night is already
    standing on the run branch, and the way to throw it away has to be on
    screen; the plan refuses before the branch is cut, which is exactly where
    `print_summary` is not.
    """
    unsatisfiable = {"T2": ticket("T2", ("T99",))}

    with pytest.raises(Refusal):
        drive(monkeypatch, repo, ["T2"], catalog=unsatisfiable)

    assert _git(repo, "branch", "--format=%(refname:short)").split() == ["main"]
    assert "git branch -D" not in capsys.readouterr().out

    drive(monkeypatch, repo, ["T1"])
    run_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    capsys.readouterr()

    with pytest.raises(Refusal):
        drive(monkeypatch, repo, ["T2"], catalog=unsatisfiable)

    commands = [
        ln.strip() for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith("git ")
    ]
    assert any(run_branch in c and "edad/t1" in c for c in commands), commands


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


# --- T008: teeth for D7-D10 -------------------------------------------------
#
# Characterization, not new behaviour. T006's ten acceptance tests all pass
# against a `session_queue` in which the breaker is checked after the ticket it
# was meant to prevent, a ticket the breaker never reached is logged `failed`,
# a second failure re-attributes the first one's skips, and the final gate runs
# no commands at all. Each was measured surviving all ten. These four pin the
# four behaviours those mutations reach, and the `mutation:` block on T008 is
# what proves they bite rather than merely run.

# Four tickets that block on nothing, so a breaker can fire with some of them
# still untouched. CHAIN cannot express that: failing any of its members skips
# the rest, and a run with nothing left unreached cannot tell `not_run` from
# `failed` no matter what it asserts.
FOUR_INDEPENDENT = tickets(("T1", ()), ("T2", ()), ("T3", ()), ("T4", ()))


def test_the_breaker_is_checked_before_the_ticket_it_would_stop(monkeypatch, repo):
    """Before, not after. Firing it after spawning the session it existed to
    prevent spends exactly the hour the budget was set to save - and a run that
    checks afterwards still reports the right breaker, so only the session count
    can tell the two apart."""
    code, session, _, _ = drive(monkeypatch, repo, ["T1", "T2"], budget_s=0)

    assert session.ran == [], f"the breaker fired but {session.ran} ran anyway"
    assert code != 0


def test_a_ticket_the_breaker_never_reached_is_not_a_failure(monkeypatch, repo):
    """The other half of D8's distinction. `test_breaker_firing_is_recorded_
    distinctly_from_a_ticket_failure` fails every ticket in its chain, so it
    never constructs one the night did not get to - and a log that called those
    `failed` would send the operator to debug a ticket that never ran."""
    state = RunState(plan_run(FOUR_INDEPENDENT, ["T1", "T2", "T3", "T4"], set()), FOUR_INDEPENDENT)

    state.fail("T1", session_log=ABORTED_WITHOUT_COMMIT)
    state.fail("T2", session_log=ABORTED_WITHOUT_COMMIT)

    log = state.as_log()
    assert log["breaker"] == "no_progress"
    assert state.remaining() == ["T3", "T4"]
    for untouched in ("T3", "T4"):
        assert log["tickets"][untouched]["status"] == "not_run", (
            f"{untouched} never ran; logging it as "
            f"{log['tickets'][untouched]['status']!r} sends the morning to debug it"
        )


def test_the_first_failure_to_reach_a_ticket_owns_its_skip(monkeypatch, repo):
    """First cause wins. A later failure that also reaches T3 must not rename
    the reason, and must not overwrite T2's own failure with a skip - the
    morning would then be told T2 never ran when in fact it is the thing that
    broke."""
    state = RunState(plan_run(CHAIN, ["T1", "T2", "T3", "T4"], set()), CHAIN)

    state.fail("T2")
    state.fail("T1")

    log = state.as_log()
    assert log["tickets"]["T2"]["status"] == "failed", "T2 failed; a later skip erased it"
    assert state.skipped["T3"] == skip_reason("T2"), "T3's reason was re-attributed to T1"


class _FakeResult:
    """What `run_commands` hands back, in the fields `final_gate` reads."""

    def __init__(self, command: str, exit_code: int = 0):
        self.command = command
        self.exit_code = exit_code
        self.ok = exit_code == 0
        self.duration_s = 0.0
        self.timed_out = False
        self.output_tail = ""


def test_the_final_gate_runs_the_commands_the_spec_names(monkeypatch, repo):
    """D10 is a measured claim, and nothing in T006 measured it: `drive`
    replaces `final_gate` wholesale, so a body that ran no commands at all
    reported `passed: True` - `all([])` - through the whole suite and `ruff`.
    This pins the wiring and the verdict."""
    declared = ["python3 -m pytest -q", "ruff check ."]
    seen: list[list[str]] = []

    def fake_run_commands(root, commands, **kw):
        seen.append(list(commands))
        return [_FakeResult(c) for c in commands]

    monkeypatch.setattr(sq, "final_gate_spec", lambda root, *_: (list(declared), 900))
    monkeypatch.setattr(sq, "run_commands", fake_run_commands)

    gate = sq.final_gate(repo, ["T1"])

    assert seen == [declared], f"the final gate ran {seen} rather than the spec's commands"
    assert [c["command"] for c in gate["commands"]] == declared
    assert gate["passed"] is True

    # And the verdict is the results', not a constant: one red command is a red
    # gate, which is the only case report_final_gate exists for.
    seen.clear()
    monkeypatch.setattr(
        sq, "run_commands", lambda root, commands, **kw: [_FakeResult(commands[0], 1)]
    )
    assert sq.final_gate(repo, ["T1"])["passed"] is False


# --- T009: the final gate's own bounds, and why the queue stopped -----------
#
# T006's review found four behaviours its tests did not hold - those are T008's,
# and they are defects in the tests. It found three more in the code, where the
# implementation matches the ticket and the ticket is what is wrong: the final
# gate borrowing an agent's hang budget for a suite no agent is in (D23), the
# gate unioned over every ticket on disk rather than the ones the run is made of
# (D24), and a queue stopped by a merge git refused saying nothing about it in
# the run log (D25). Spec-level, so they are amended and re-run.

# T006 raised its own `command_timeout_s` to 1800 for reasons that were entirely
# about how long an agent may hang. Nothing would choose the number below as a
# test suite's bound, which is exactly the point: whatever a ticket allows its
# agent, the final gate's bound is not that.
AGENT_HANG_BUDGET_S = 4242


def write_ticket(
    root: Path, tid: str, full_gate: list[str], hang_budget_s: int | None = None
) -> None:
    """A real ticket on disk, because the defect D24 names is a glob. A catalog
    injected at the `load_ticket` seam cannot show a ticket the run never queued
    reaching the gate anyway - the seam is the thing being accused."""
    lines = ["---", f"id: {tid}", "full_gate:"]
    lines += [f"  - {command}" for command in full_gate]
    if hang_budget_s is not None:
        lines += ["kill_conditions:", f"  command_timeout_s: {hang_budget_s}"]
    lines += ["---", "", f"{tid} body.", ""]
    (root / ".edad" / "tickets" / f"{tid}.md").write_text("\n".join(lines))


def latest_run_log(root: Path) -> dict:
    return json.loads(sorted((root / ".edad" / "runs").glob("*.json"))[-1].read_text())


def test_final_gate_timeout_is_its_own_bound(repo):
    """D23. `command_timeout_s` is a `kill_conditions` entry: it bounds how long
    an *agent* may hang inside a session. The final gate is a suite the
    controller runs with no agent in it, so borrowing that number means a ticket
    that raised its own timeout - as T006 did, for measured reasons entirely
    about the agent - silently changes how long the night's last gate may take.
    Two quantities under one name is how a wall-clock budget stops meaning
    anything: the operator sets a bound, the queue honours it, and then the
    epilogue runs on a bound nobody chose."""
    write_ticket(repo, "T1", ["python3 -m pytest -q"], hang_budget_s=AGENT_HANG_BUDGET_S)

    _, timeout_s = sq.final_gate_spec(repo, ["T1"])

    assert timeout_s == sq.FINAL_GATE_TIMEOUT_S, (
        f"the final gate ran on {timeout_s}s, which is not the bound chosen for it"
    )
    assert timeout_s != AGENT_HANG_BUDGET_S, "the final gate is on the agent's hang budget"


def test_final_gate_spec_unions_only_the_queued_tickets(repo):
    """The union of `full_gate` across the ids the run is made of, first-seen
    order, deduplicated as it already is. Order follows the queue rather than
    the filename sort, and that is what tells the two apart: a spec that globs
    `.edad/tickets/*.md` answers in directory order whatever it was asked."""
    write_ticket(repo, "T1", ["python3 -m pytest -q", "ruff check ."])
    write_ticket(repo, "T2", ["ruff check .", "python3 -m mypy ."])

    commands, _ = sq.final_gate_spec(repo, ["T2", "T1"])

    assert commands == ["ruff check .", "python3 -m mypy .", "python3 -m pytest -q"]


def test_a_ticket_outside_the_run_cannot_widen_the_final_gate(repo):
    """D24. Today every ticket declares the same two commands, so the glob is
    invisible; the first one to declare a third joins the final gate of every run
    that does not include it, and the night ends measuring a claim about code it
    never touched. Taking the gate from the tickets rather than a second list was
    right - D10's reason, that a gate kept beside the one the tickets carry is a
    place for the two to disagree - but "the tickets" means the ones the run is
    made of."""
    write_ticket(repo, "T1", ["python3 -m pytest -q"])
    write_ticket(repo, "T2", ["python3 -m pytest -q"])
    write_ticket(repo, "T9", ["python3 -m pytest -q", "cargo test --all"])

    commands, _ = sq.final_gate_spec(repo, ["T1", "T2"])

    assert commands == ["python3 -m pytest -q"], (
        f"T9 is on disk and not in this run, and the gate came back {commands}"
    )


def test_the_run_log_names_why_the_queue_stopped(monkeypatch, repo):
    """D25. A merge git refused stops the queue, and the log records the ticket
    `promoted, merge refused`, everything behind it `not_run`, and `breaker:
    null` - three true statements that never say why. The controller has the
    reason and prints it, but stdout is the scatter the run log replaced: D9
    exists because correlating a night by hand at 8am is what one file is for."""

    class DivergingSession(FakeSession):
        def __call__(
            self, root: Path, ticket_id: str, sandbox: str = "none", network: str | None = None
        ) -> int:
            code = super().__call__(root, ticket_id, sandbox, network)
            # Move the run branch on after the ticket branched, so the merge
            # back can no longer fast-forward.
            (root / "src" / "drift.py").write_text(f"# drifted before {ticket_id}\n")
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", "concurrent work")
            return code

    drive(monkeypatch, repo, ["T1", "T2"], session=DivergingSession())

    log = latest_run_log(repo)
    assert log["tickets"]["T1"]["status"] == "promoted, merge refused"
    assert log["tickets"]["T2"]["status"] == "not_run"
    assert log["breaker"] is None, "no breaker fired here; the merge is what stopped it"
    assert log["stopped_because"] and "edad/t1" in log["stopped_because"], (
        f"the log does not name the refused merge: {log['stopped_because']!r}"
    )

    # And a breaker sets it *alongside* `breaker` rather than instead of it.
    # `breaker` stays the separate key D9 required; `stopped_because` is the
    # wider question, the one a merge refusal also answers.
    state = RunState(plan_run(FOUR_INDEPENDENT, ["T1", "T2", "T3", "T4"], set()), FOUR_INDEPENDENT)
    state.fail("T1", session_log=ABORTED_WITHOUT_COMMIT)
    state.fail("T2", session_log=ABORTED_WITHOUT_COMMIT)

    stopped = state.as_log()
    assert stopped["breaker"] == "no_progress"
    assert stopped["stopped_because"] and "no_progress" in stopped["stopped_because"], (
        f"the breaker fired and the log says {stopped['stopped_because']!r}"
    )


def test_a_clean_drain_names_no_stop_reason(monkeypatch, repo):
    """`None`, rather than a string saying nothing went wrong. The key answers
    "why did this stop", and a night that drained did not stop - a reader made
    to parse prose to learn there was no incident is back to correlating by
    hand, which is the thing D9 exists to end."""
    code, _, _, _ = drive(monkeypatch, repo, ["T1", "T2"])

    log = latest_run_log(repo)
    assert code == 0
    assert log["breaker"] is None
    assert log["stopped_because"] is None, (
        f"the queue drained; {log['stopped_because']!r} is not a stop"
    )


# --- the sandbox tier: D1, D4, D5 (plan-time), D6, D17 (queue) --------------
#
# T013. The controller spawned every child with no --sandbox and no --network,
# so the overnight run - the one path where nobody is watching - was the one
# path that never sandboxed. These tests are about the queue carrying the tier,
# and about what a docker night does before it cuts its branch.


class Docker:
    """The three names a docker night reaches through `edad.session` and
    `edad.egress`, replaced at this module so the test observes the order.

    `calls` records (name, branch root was on) as each is reached, because the
    contract is positional: the proxy is ensured, then the network validated,
    and both happen while root is still on `main` - before the run branch is
    cut and before any session is spawned. `created` is what `ensure` reports,
    and `refuse` is what `validate` raises, if anything.
    """

    def __init__(self, created: bool = True, refuse: Exception | None = None):
        self.created = created
        self.refuse = refuse
        self.calls: list[tuple[str, str]] = []
        self.networks: list[str | None] = []

    def _on(self, root: Path) -> str:
        return _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()

    def install(self, monkeypatch, root: Path) -> Docker:
        def ensure(network: str) -> bool:
            self.calls.append(("ensure", self._on(root)))
            self.networks.append(network)
            return self.created

        def validate(sandbox: str, network: str | None, *args, **kwargs) -> list[str]:
            self.calls.append(("validate", self._on(root)))
            self.networks.append(network)
            if self.refuse is not None:
                raise self.refuse
            return []

        def remove(network: str) -> None:
            self.calls.append(("remove", self._on(root)))
            self.networks.append(network)

        monkeypatch.setattr(sq, "ensure_egress_proxy", ensure)
        monkeypatch.setattr(sq, "validate_network", validate)
        monkeypatch.setattr(sq, "remove_egress_proxy", remove)
        return self

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def run_branches(root: Path) -> list[str]:
    out = _git(root, "branch", "--list", "edad/run-*")
    return [b.strip("* ").strip() for b in out.splitlines() if b.strip()]


def back_to_main(root: Path) -> None:
    """Between two nights in one test. The run branch is named by the second,
    so the one just finished is deleted rather than left to collide."""
    _git(root, "checkout", "-q", "main")
    for branch in run_branches(root):
        _git(root, "branch", "-q", "-D", branch)


def test_the_queue_parser_takes_sandbox_and_network():
    """D1. One decision per night, uniform across the queue: the flags are the
    session's own, choices included, so an operator who knows one entry point
    knows the other."""
    args = sq.build_parser().parse_args(
        ["run", "T1", "T2", "--sandbox", "docker", "--network", "edad-fixtures"]
    )
    assert args.sandbox == "docker"
    assert args.network == "edad-fixtures"
    assert args.tickets == ["T1", "T2"]

    # The session's choices, not a free string: a typo must be refused by the
    # parser rather than reach a child that refuses it N times.
    with pytest.raises(SystemExit):
        sq.build_parser().parse_args(["run", "T1", "--sandbox", "podman"])


def test_session_argv_carries_the_network_to_every_child(monkeypatch, repo):
    """D1. The builder emits `--network <name>` exactly once when a name is
    given, and the queue hands the tier to every child - the third as much as
    the first, and never "only when it is not the default"."""
    argv = session_argv("T1", sandbox="docker", network="edad-fixtures")
    assert argv[:5] == [sys.executable, "-m", "edad.session", "run", "T1"]
    assert argv.count("--network") == 1
    assert argv[argv.index("--network") + 1] == "edad-fixtures"
    assert argv[argv.index("--sandbox") + 1] == "docker"
    assert "--network" not in session_argv("T1", sandbox="none")

    Docker().install(monkeypatch, repo)
    _, session, _, _ = drive(
        monkeypatch, repo, ["T1", "T2", "T3"], sandbox="docker", network="edad-fixtures"
    )

    assert session.ran == ["T1", "T2", "T3"]
    assert session.tiers == [("docker", "edad-fixtures")] * 3


def test_the_queue_parser_defaults_to_no_sandbox():
    """D4. Parity with `session.py`: a night nobody thought about changes
    nothing. Defaulting to docker would promote a default onto a code path that
    has never executed overnight."""
    args = sq.build_parser().parse_args(["run", "T1"])
    assert args.sandbox == "none"
    assert args.network is None


def test_the_run_log_records_the_tier(monkeypatch, repo):
    """D4. "Did this night run sandboxed" is a lookup, not a reconstruction
    from N session logs. Top-level, beside `run_branch`, on every night - the
    default one says `none` rather than saying nothing."""
    drive(monkeypatch, repo, ["T1"])
    log = latest_run_log(repo)
    assert log.get("sandbox") == "none", f"the log does not name the tier: {sorted(log)}"
    assert "network" in log and log["network"] is None
    assert "run_branch" in log

    state = RunState(
        plan_run(FOUR_INDEPENDENT, ["T1"], set()), FOUR_INDEPENDENT,
        sandbox="docker", network="edad-fixtures",
    )
    payload = state.as_log()
    assert payload["sandbox"] == "docker"
    assert payload["network"] == "edad-fixtures"


def test_session_argv_states_the_tier_even_when_it_is_the_default(monkeypatch, repo):
    """D6. `--sandbox none` is in the child's argv when nothing asked for it.
    Otherwise the tier is asserted by two files' defaults agreeing, and a
    running night's `ps` output would not say which tier it is."""
    default = session_argv("T1")
    assert default.count("--sandbox") == 1
    assert default[default.index("--sandbox") + 1] == "none"
    assert session_argv("T1", sandbox="none", network=None) == default
    assert "--network" not in default

    # And the queue passes what it carries, default included - `work_one`
    # never decides that the default is not worth mentioning.
    _, session, _, _ = drive(monkeypatch, repo, ["T1", "T2"])
    assert session.tiers == [("none", None), ("none", None)]


def test_a_docker_night_validates_the_network_before_the_branch_is_cut(monkeypatch, repo):
    """D5, the plan-time half. A misnamed network costs seconds at ticket 0
    rather than a night of instant aborts that the run log reports as "ran out
    of tickets". So: proxy ensured, then network validated, both with root on
    `main` and no `edad/run-*` in existence, and only then a branch and a
    session. A `none` night asks docker nothing at all."""
    docker = Docker().install(monkeypatch, repo)

    _, session, _, _ = drive(monkeypatch, repo, ["T1"], sandbox="docker", network="edad-fixtures")

    assert docker.names()[:2] == ["ensure", "validate"]
    assert docker.calls[0] == ("ensure", "main")
    assert docker.calls[1] == ("validate", "main")
    assert docker.networks[:2] == ["edad-fixtures", "edad-fixtures"]
    assert session.ran == ["T1"]

    # The contrast: the default tier reaches none of the three.
    quiet = Docker().install(monkeypatch, repo)
    back_to_main(repo)
    drive(monkeypatch, repo, ["T2"])
    assert quiet.calls == []


def test_a_refused_network_stops_the_run_at_ticket_zero(monkeypatch, repo):
    """D5. `validate_network`'s `Abort` and the lifecycle's `EgressError` are
    the queue's own `Refusal`, so `main()` prints and exits 2 as for every
    other refusal - and the refusal happens before anything exists: no branch
    cut, no session spawned, HEAD still on `main`."""
    before = _git(repo, "rev-parse", "HEAD").strip()
    docker = Docker(refuse=sq.Abort("network edad-fixtures is not internal")).install(
        monkeypatch, repo
    )

    spawned = FakeSession()
    with pytest.raises(Refusal, match="not internal"):
        drive(
            monkeypatch, repo, ["T1", "T2"], session=spawned,
            sandbox="docker", network="edad-fixtures",
        )

    assert spawned.ran == []
    assert docker.names() == ["ensure", "validate", "remove"], (
        "ensure, refuse, and tear down what this run created"
    )
    assert run_branches(repo) == []
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert _git(repo, "rev-parse", "HEAD").strip() == before

    # A proxy that could not be ensured is the same refusal, and validation is
    # never reached without it.
    failed = Docker().install(monkeypatch, repo)

    def cannot_ensure(network: str) -> bool:
        failed.calls.append(("ensure", "main"))
        raise sq.EgressError("edad-egress image is not built")

    monkeypatch.setattr(sq, "ensure_egress_proxy", cannot_ensure)
    with pytest.raises(Refusal, match="not built"):
        drive(
            monkeypatch, repo, ["T1"], session=spawned,
            sandbox="docker", network="edad-fixtures",
        )
    assert failed.names() == ["ensure"]
    assert spawned.ran == []
    assert run_branches(repo) == []


def test_the_queue_ensures_the_proxy_before_the_run_and_removes_it_after(monkeypatch, repo):
    """D17, the queue half. One proxy per night: ensured once before the first
    session, removed once after the last - in the same `finally` that writes
    the run log, so a refusal mid-queue tears it down too."""
    docker = Docker(created=True).install(monkeypatch, repo)

    _, session, _, _ = drive(
        monkeypatch, repo, ["T1", "T2"], sandbox="docker", network="edad-fixtures"
    )

    assert session.ran == ["T1", "T2"]
    assert docker.names() == ["ensure", "validate", "remove"]
    assert docker.networks == ["edad-fixtures"] * 3
    assert (repo / ".edad" / "runs").exists(), "the run log was written on the same exit path"

    # Mid-queue refusal: the run log is written and the proxy is removed anyway.
    torn = Docker(created=True).install(monkeypatch, repo)
    back_to_main(repo)

    def refuse_on_the_second(root: Path, tkt: dict) -> None:
        if tkt["id"] == "T4":
            raise Refusal("T4 cannot be re-approved")

    monkeypatch.setattr(sq, "run_session", FakeSession())
    monkeypatch.setattr(sq, "reapprove", refuse_on_the_second)
    monkeypatch.setattr(sq, "load_ticket", lambda root, tid: ticket(tid))
    monkeypatch.setattr(sq, "final_gate", lambda root, *_: {"passed": True, "commands": []})
    with pytest.raises(Refusal):
        sq.run_queue(repo, ["T3", "T4"], sandbox="docker", network="edad-fixtures")
    assert torn.names() == ["ensure", "validate", "remove"]


def test_the_queue_leaves_a_proxy_it_did_not_create(monkeypatch, repo):
    """D17. Whoever created the proxy destroys it. A proxy already present
    when the night started - an operator's, or a concurrent run's - is not this
    run's to remove."""
    docker = Docker(created=False).install(monkeypatch, repo)

    _, session, _, _ = drive(
        monkeypatch, repo, ["T1", "T2"], sandbox="docker", network="edad-fixtures"
    )

    assert session.ran == ["T1", "T2"]
    assert docker.names() == ["ensure", "validate"]
    assert "remove" not in docker.names()

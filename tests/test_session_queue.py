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

T004's decisions are here. T005's (D1, D6, D12) and T006's (D7, D8, D9, D10)
were drafted against this same design and are recoverable in full from commit
`1a48d51`; re-author them from there when their ticket comes up.

`run_queue` is exercised against a real repository, because every claim it
makes is about git topology: that a branch was cut from `main`, that `main` did
not move, that a merge was a fast-forward. Those are exactly the claims an argv
assertion passes while composing wrongly, the trap `test_gate_mutation.py`
names. The session and the re-approval are replaced at their named seams, the
pattern `run_commands` and `docker_network_internal` established: git is real
here and the agent is not.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from edad import session_queue as sq
from edad.session_queue import Refusal, discard_command, outcome_of, session_argv

# --- fixtures ---------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


def ticket(tid: str, blocked_by: tuple[str, ...] = ()) -> dict:
    return {"id": tid, "blocked_by": list(blocked_by), "frozen": [], "scope": []}


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

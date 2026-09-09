"""The multi-ticket controller: a queue of approved tickets run on one branch.

`edad.session` runs exactly one ticket and refuses to merge. That refusal keeps
unreviewed code off a mainline, and it is also the wall a chain of dependent
tickets hits: `preflight` wants the blocker's evidence record at the repo root,
`make_worktree` branches from root HEAD, and only a merge satisfies both. So
every pair of dependent tickets needs a human awake between them, which is the
whole cost of an otherwise unattended run.

This controller cuts an integration branch from `main`, checks it out in root,
and works the queue against it. Because root HEAD *is* the run branch, both
conditions are met by the merge the controller performs itself, and neither
`preflight` nor `make_worktree` changes at all. `main` never moves: the
promotion bar proves a ticket broke nothing, not that the code is any good, so
what the night produces is a branch for a human to read.

The file is authored one ticket at a time, deliberately. Every decision in the
spec attaches to this one module, so a file written whole would leave each
ticket's `full_gate` facing the whole chain's tests: the baseline taken before
the module exists holds a single collection-error key, and the moment the
module appears that key becomes one node id per test - new failures the ratchet
has never seen and cannot absorb.

T004's decisions are here: D2 the integration branch, D3 fast-forward merges,
D4 one subprocess per ticket, D5 just-in-time re-approval, D11 where the run
leaves you. T005's are here too: D1 the queue is a plan computed before
anything executes, D6 doneness is the evidence record's existence, D12
re-invoking the same command resumes. T006's (D7, D8, D9, D10) are present as
signatures only - the block below `# --- T006` is stubs, standing in for work
that ticket has not done yet, and every one of them returns the wrong answer on
purpose.

D13 is unenforced: v1 is sequential. `plan_run` computes `Plan.independent`
and nothing acts on it, so a later scheduler is a change rather than a
redesign - and so the wall-clock parallelism would have saved is measurable
rather than assumed.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from edad.gate import check_freeze, load_ticket, repo_root

MAIN_BRANCH = "main"
# HEAD is the whole memory of a run (D12), so the prefix is what tells a
# resumable branch from anything else root might be standing on.
RUN_BRANCH_PREFIX = "edad/run-"


class Refusal(Exception):
    """The run cannot start, or cannot continue. Raised before - or instead of
    - doing damage. `run_queue` prints the summary and the discard command on
    the way past, then lets it out: the operator reading the morning's
    scrollback still needs to learn why the night stopped."""


# --- pure builders ----------------------------------------------------------


def session_argv(ticket_id: str) -> list[str]:
    """The command that runs one ticket. A subprocess, not an import.

    `gate.die()` raises a bare `SystemExit(2)` from twenty-five sites that
    `session.main()` does not catch, so one bad ticket in-process takes the
    whole night down. And a session holds the `edad.gate` it imported at start,
    which is what makes the verifier the pre-session code: a controller that
    imported once and looped would freeze that snapshot for the entire run, so
    the final state of the run branch would never have been judged by its own
    gate.
    """
    return [sys.executable, "-m", "edad.session", "run", ticket_id]


def outcome_of(exit_code: int) -> str:
    """`session.main()` already returns 0 exactly when the outcome is in
    PROMOTED_OUTCOMES, so the exit code is the whole signal - and parsing
    stdout for a second one would invent a channel that can disagree."""
    return "promoted" if exit_code == 0 else "failed"


def discard_command(
    run_branch: str,
    ticket_branches: Sequence[str],
    worktree_paths: Sequence[str] = (),
) -> str:
    """The literal command that throws the night away.

    Naming only the run branch is a false rollback: every `edad/t00N` branch
    still points at all of the work, and the operator who ran the printed
    command believes it is gone.

    Each worktree is removed *before* the branch it holds, because a
    worktree-held branch cannot be deleted at all. Without the removals git
    deletes the run branch, refuses every `edad/t00N`, and exits non-zero -
    having destroyed the one ref the night was recoverable from while leaving
    all of the work reachable.

    `worktree_paths` is keyword-defaulted last so every existing two-argument
    call is unchanged, and it takes paths rather than ticket ids so the
    branch-to-worktree mapping is not restated backwards by a caller that
    already knows it.
    """
    steps = [f"git worktree remove --force {shlex.quote(str(p))}" for p in worktree_paths]
    # The run branch is checked out in root, and git will not delete the branch
    # it is standing on.
    steps.append(f"git checkout -q {shlex.quote(MAIN_BRANCH)}")
    refs = " ".join(shlex.quote(b) for b in [run_branch, *ticket_branches])
    steps.append(f"git branch -D {refs}")
    return " && ".join(steps)


def new_run_branch() -> str:
    return RUN_BRANCH_PREFIX + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


# --- D1: the queue is a plan, made before anything runs ---------------------


@dataclass
class Plan:
    """What the run will do, decided before any of it happens.

    `order` is the ids to run, topologically sorted. `done` is the ids the
    operator asked for that were already finished - dropped from the queue,
    never re-run and never re-approved. `independent` groups ids that block on
    none of each other.

    Nothing acts on `independent`; v1 is sequential (D13). It is computed and
    recorded anyway, so that a later scheduler is a change rather than a
    redesign, and so the wall-clock parallelism would have saved is measurable
    rather than assumed.
    """

    order: list[str] = field(default_factory=list)
    done: set[str] = field(default_factory=set)
    independent: list[set[str]] = field(default_factory=list)


def blockers_of(tickets: dict[str, dict], ticket_id: str) -> list[str]:
    """`blocked_by`, defaulted the way `preflight` defaults it
    (`edad/session.py:109`) so the controller and the session cannot disagree
    about what an absent or null field means.

    Tolerant of an id with no loaded ticket, because the common case is exactly
    that: a queued ticket names a blocker that is not itself queued, and whether
    that is fine is `plan_run`'s question, not this one's.
    """
    return list((tickets.get(ticket_id) or {}).get("blocked_by") or [])


def plan_run(tickets: dict[str, dict], requested: Sequence[str], done: set[str]) -> Plan:
    """Order the queue by the tickets' own `blocked_by`; refuse the impossible.

    Pure, and called before the run branch is cut, so a doomed queue costs
    seconds rather than a night: an operator who types the tickets in the wrong
    order would otherwise fail on the first dependency, and one who types a
    ticket whose blocker is nowhere would get the same failure hours in.

    Ordering comes from `blocked_by` rather than from argument order, and
    rather than from a run manifest - a manifest would put ordering in a second
    place beside `blocked_by`, where the two can disagree.
    """
    queued = list(dict.fromkeys(requested))
    queued_set = set(queued)

    # Checked before the cycle: a blocker that is nowhere is a typo or a ticket
    # nobody wrote, and naming it is a better answer than naming a cycle it may
    # also happen to sit in. Only the unsatisfiable ones are named - a message
    # listing every blocker sends the operator off to check the ones that were
    # already fine.
    unsatisfiable: list[str] = []
    for ticket_id in queued:
        for blocker in blockers_of(tickets, ticket_id):
            if blocker in queued_set or blocker in done or blocker in unsatisfiable:
                continue
            unsatisfiable.append(blocker)
    if unsatisfiable:
        raise Refusal(
            "this queue can never be satisfied: "
            + ", ".join(unsatisfiable)
            + " - neither queued here nor carrying an evidence record. Queue "
            "them ahead of the tickets they block, or run them first."
        )

    # A done ticket is not run, so it constrains nothing, and a blocker
    # satisfied from disk was never in the queue to be ordered against.
    to_run = [ticket_id for ticket_id in queued if ticket_id not in done]
    running = set(to_run)
    remaining = {t: {b for b in blockers_of(tickets, t) if b in running} for t in to_run}

    plan = Plan()
    while remaining:
        # Equal depth means no path between them - a path would deepen one end
        # - so a level is exactly a group of mutually independent ids. Taken in
        # the operator's own order within the level, which is the one place
        # argument order can still be honoured without contradicting a blocker.
        level = [t for t in to_run if t in remaining and not remaining[t]]
        if not level:
            raise Refusal(
                "blocked_by has a cycle among: "
                + ", ".join(sorted(remaining))
                + ". No order satisfies it, so the run refuses now rather than "
                "discovering it on the first dependency hours in."
            )
        plan.order.extend(level)
        plan.independent.append(set(level))
        for ticket_id in level:
            del remaining[ticket_id]
        for deps in remaining.values():
            deps.difference_update(level)

    # What the operator asked for and had already finished. A blocker satisfied
    # from disk is not a member: they never asked for it, so reporting it as a
    # skipped ticket would answer a question nobody put.
    plan.done = {ticket_id for ticket_id in queued if ticket_id in done}
    return plan


# --- D6: doneness is the evidence record's existence ------------------------


def evidence_path(root: Path, ticket_id: str) -> Path:
    """Where `promote_evidence` lands a record (`edad/session.py:584`) and where
    `preflight` looks for a blocker's (`:110`). One expression, so the
    controller and the session cannot drift into disagreeing about what doneness
    looks like on disk."""
    return Path(root) / ".edad" / "evidence" / f"{ticket_id}.json"


def is_done(root: Path, ticket_id: str) -> bool:
    """The file existing is the whole answer.

    `promote_evidence` is only ever called on a promoted outcome
    (`edad/session.py:680`), so a record on disk already means promoted and any
    field read to confirm it is redundant. At worst it is wrong: the records on
    disk are not uniform, and reading one of their keys as a verdict marks a
    passing ticket not-done. See `verdict`.
    """
    return evidence_path(root, ticket_id).exists()


def verdict(record: dict) -> str:
    """How a done ticket passed - never whether it did.

    `pre_existing_only` returns False whenever `commands_ok` is True
    (`edad/gate.py:219`), so `passed_modulo_baseline` is False on T003, which
    passed cleanly. False there means *the baseline was not needed*, not
    *failed*. A controller reading that key as a verdict marks a passing ticket
    not-done - a silent wrong answer, strictly worse than a loud KeyError. So
    the verdict is taken from `passed`, and the modulo case is what remains.
    """
    return "passed" if record.get("passed") else "passed modulo baseline"


def field_display(record: dict, key: str) -> str:
    """Absent is not false.

    `mutation_proof` is missing from all three records on disk - the field
    postdates every session run so far - and rendering that as "no" reports a
    proof as having failed when it was never attempted.
    """
    value = record.get(key)
    return "not recorded" if value is None else str(value)


def done_set(root: Path, requested: Sequence[str], tickets: dict[str, dict]) -> set[str]:
    """Every id the plan is about to reason about that carries a record: the
    queued ids **and the blockers they name**.

    Probing only the queued ids refuses a run whose blocker is finished but was
    not re-typed, and reports that blocker as having no evidence record when it
    has one. The workaround - re-listing every finished ancestor on every
    invocation - is the remembered state D12 exists to abolish.
    """
    ids = set(requested)
    for ticket_id in requested:
        ids.update(blockers_of(tickets, ticket_id))
    return {ticket_id for ticket_id in ids if is_done(root, ticket_id)}


# --- git --------------------------------------------------------------------


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def branch_exists(root: Path, branch: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def current_branch(root: Path) -> str:
    return git(root, "rev-parse", "--abbrev-ref", "HEAD")


def is_ancestor(root: Path, ref: str, of: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", ref, of],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def start_branch(root: Path) -> tuple[bool, str]:
    """Where this invocation runs, and whether a night is already standing.

    HEAD is the whole memory of a run, which is what makes re-invoking the exact
    same command a resume. An `--into <branch>` flag would be correct, and it
    would be a flag to look up at exactly the moment the operator is annoyed and
    half-awake.

    Returns `(resumed, run_branch)`; on a fresh start the branch is not named
    here, because it must not be cut until the plan has been made.
    """
    head = current_branch(root)
    if head == MAIN_BRANCH:
        return False, ""
    if head.startswith(RUN_BRANCH_PREFIX):
        return True, head
    raise Refusal(
        f"root is on {head}, which is neither {MAIN_BRANCH} nor an "
        f"{RUN_BRANCH_PREFIX}* branch. A queue run on top of one half-finished "
        "session's branch is not a state anything here knows how to reason "
        "about; check out the run branch you meant to continue, or "
        f"{MAIN_BRANCH} to start a new one."
    )


def rollback_branches(root: Path, run_branch: str, created: Sequence[str] = ()) -> list[str]:
    """Everything the night put on the run branch, plus what it left beside it.

    The rollback covers the run branch, not the invocation. A resumed run
    rebuilds its own bookkeeping empty and a done ticket never reaches the probe
    that fills it, so a discard naming only what *this* invocation created would
    leave the first invocation's branches pointing at all of that work - the
    false rollback D11 already refused, back within reach because D12 exists.

    So the set is derived from the branch rather than from the invocation:
    reachable from the run branch and **not** from `main`. Not "merged into the
    run branch" - every ticket branch of every earlier chain is an ancestor of
    `main`, and so of the run branch, and a discard built that way deletes them
    all. `created` covers the other direction: a branch this invocation made and
    did not merge is reachable from neither.
    """
    branches = set(created)
    listed = git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/edad/t*")
    for branch in listed.split():
        if is_ancestor(root, branch, run_branch) and not is_ancestor(root, branch, MAIN_BRANCH):
            branches.add(branch)
    return sorted(branches)


def registered_worktrees(root: Path) -> set[Path]:
    """The worktrees git itself knows about, resolved.

    Asked of git rather than of the filesystem, because a directory existing is
    a weaker claim than git holding it: `git worktree remove` on a path git
    never registered fails, and that failure aborts the discard command's `&&`
    chain on its first step, deleting nothing at all.
    """
    out = subprocess.run(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return {
        Path(line.removeprefix("worktree ")).resolve()
        for line in out.splitlines()
        if line.startswith("worktree ")
    }


# --- re-approval ------------------------------------------------------------


def check_reapprovable(root: Path, ticket: dict, drifted: list[str] | None = None) -> None:
    """Refuse to re-approve a contract that has drifted since a human read it.

    `check_freeze` already compares both the frozen file hashes and the
    ticket's own bytes - `scope`, `acceptance` and `kill_conditions` are part
    of the contract too, and drift there is the same laundering with a wider
    blast radius - so deriving `drifted` from it covers both. The parameter
    exists so the refusal is testable without building an approval lock first.
    """
    if drifted is None:
        _, drifted = check_freeze(root, ticket)
    if drifted:
        raise Refusal(
            f"{ticket['id']} cannot be re-approved; approving over this would launder "
            "the drift into the contract:\n  " + "\n  ".join(drifted)
        )


def reapprove(root: Path, ticket: dict) -> None:
    """Refresh this ticket's approval lock against the run branch.

    The lock pins a `full_gate_baseline`, and a ticket may promote if it added
    no new failures - so a baseline taken before the run only ever goes stale
    in the permissive direction. If the first ticket fixes a long-red test at
    1am, the third ticket's pre-run baseline still lists it, that agent may
    break it again, and the ratchet waves the regression through as
    pre-existing. Refreshing per ticket, on the run branch, closes that;
    `refuse_silent_widening` handles the other direction, refusing a baseline
    that grew.

    `check_reapprovable` runs **first**, and the order is the decision:
    approval rewrites the lock from the current tree, so running it over a
    drifted file would write the drift into the contract as though a human had
    read it.
    """
    check_reapprovable(root, ticket)
    proc = subprocess.run(
        [sys.executable, "-m", "edad.gate", "approve", ticket["id"]], cwd=root, check=False
    )
    if proc.returncode != 0:
        raise Refusal(
            f"re-approving {ticket['id']} on the run branch exited {proc.returncode}; "
            "the run stops rather than spawning a session against a lock that was "
            "never rewritten"
        )


def run_session(root: Path, ticket_id: str) -> int:
    """Spawn one ticket's session and hand back its exit code."""
    return subprocess.run(session_argv(ticket_id), cwd=root, check=False).returncode


# --- T006: D7, D8, D9, D10 - signatures only --------------------------------
#
# Deliberately wrong, and here only so `approve` can take a red proof that
# means something. With the module importable, each frozen test reaches its
# assertions and fails there - `FAILED <file>::<test>` - rather than dying in
# collection. An `ERROR` says an import broke, which is true of a test that
# asserts nothing just as readily, so it proves nothing about the assertions
# the freeze is meant to hold the agent to.
#
# Every return below is a value no acceptance test accepts. That is the point:
# a stub that happened to satisfy its test would be a passing acceptance
# command at approve time, which is refused for the same reason.


def skip_reason(failed_id: str) -> str:
    """Names the ticket that caused the skip, so the morning's triage is one
    line rather than a reconstruction of the dependency graph."""
    return ""


def no_commit_abort(session_log: dict) -> bool:
    """True when a session log shows a non-promoted outcome and no iteration
    that made a commit.

    Reads the log `edad.session` already writes - `asdict(SessionLog)`, so
    `outcome` against `PROMOTED_OUTCOMES` and `iterations[].made_commit`. An
    agent that exits non-zero and commits nothing is not failing the ticket, it
    is not running.
    """
    return False


def breaker_fired(
    *, no_commit_aborts: int, elapsed_s: float, budget_s: float | None = None
) -> str | None:
    """`"no_progress"`, `"wall_clock"`, or `None`. Pure, keyword-only.

    `budget_s=None` means no wall-clock bound. Wall-clock is the bound the
    operator agreed to when they went to bed; it does not bound the bill, and
    nothing here does.
    """
    return None


class RunState:
    """The fold: what the night has done so far, and whether it may continue.

    `fail(ticket_id, session_log=None)` records the failure and propagates
    skips to everything transitively reachable from it through `blocked_by` -
    reachability, not the immediate edge. `skipped` maps a skipped id to its
    reason, `remaining()` lists what is still to run, and `breaker` stays
    `None` until one fires.

    `as_log()` returns the run-log payload with `breaker` a **separate key**
    from `tickets`: a ticket the breaker never reached did not fail, and a log
    that conflated the two would send the operator to debug a ticket that never
    ran.
    """

    def __init__(self, plan: Plan, tickets: dict[str, dict], budget_s: float | None = None):
        self.plan = plan
        self.tickets = tickets
        self.budget_s = budget_s
        self.skipped: dict[str, str] = {}
        self.breaker: str | None = None

    def fail(self, ticket_id: str, session_log: dict | None = None) -> None:
        return None

    def remaining(self) -> list[str]:
        return []

    def as_log(self) -> dict:
        return {"tickets": {}, "breaker": None, "final_gate": None}


# --- the driver -------------------------------------------------------------


def report_merge_refusal(branch: str, run: str, merge: subprocess.CompletedProcess) -> None:
    """Git's own words, verbatim.

    Git already answered the question this controller cannot: `--ff-only` fails
    on divergence and on a dirty root tree alike, only git knows which, and
    only git names the blocking path. A fixed "something ran concurrently"
    sends a half-awake operator hunting a concurrent run that never happened,
    while the real cause - one uncommitted edit - is the one thing not on
    screen.
    """
    print(f"\n{branch} would not fast-forward onto {run}. git said:")
    if merge.stdout.strip():
        print(merge.stdout.rstrip())
    if merge.stderr.strip():
        print(merge.stderr.rstrip(), file=sys.stderr)
    print(
        "The run stops here. Whatever git named above, resolving it by hand would "
        "put code on the run branch that no full_gate ever judged."
    )


def print_summary(
    run_branch: str,
    results: list[tuple[str, str, str | None]],
    ticket_branches: list[str],
    worktree_paths: list[str],
) -> None:
    """The summary and the rollback, printed on every exit path that has a run
    branch - a clean finish, a merge git refused, a `Refusal` raised between two
    tickets, and a plan-stage `Refusal` on a *resumed* run alike, because
    whatever already merged is sitting on the run branch in all four.

    Not on a plan-stage refusal from a fresh cut: the plan refuses before the
    branch exists, so a discard command there names a branch that was never
    created, `git branch -D` fails, and the operator watches a rollback that
    could not have worked. The discard command goes last, so it is the final
    runnable line."""
    print(f"\nrun branch: {run_branch}")
    if not results:
        print("  (no ticket ran)")
    for ticket_id, outcome, sha in results:
        print(f"  {ticket_id}: {outcome}" + (f", merged {sha[:12]}" if sha else ""))
    print("\nto discard this run entirely:")
    print("  " + discard_command(run_branch, ticket_branches, worktree_paths))


def final_gate(root: Path) -> dict:
    """One full gate on the run branch tip, after the queue drains.

    Redundant by derivation from the fast-forward property - ticket N's gate
    already ran against every earlier ticket's merged code - and it runs anyway,
    because this harness holds that a measured claim beats a derived one. If it
    ever fails while every ticket promoted, the derivation is wrong somewhere;
    the run log reports it and nothing else is designed.
    """
    return {"passed": False, "commands": []}


def run_queue(root: Path, ticket_ids: list[str]) -> int:
    """Plan the queue, then work it on an integration branch.

    The plan comes first and is made from the tickets' own `blocked_by` (D1),
    before the branch is cut and before any session is spawned, so a cycle or a
    blocker that is nowhere costs seconds rather than a night.

    Where it runs is read off HEAD (D12): `main` cuts a fresh `edad/run-*`,
    an existing `edad/run-*` is continued on, and anything else refuses. Tickets
    already carrying an evidence record (D6) are dropped from the queue - never
    re-run, and never re-approved, since re-approval rewrites `approved_at` and
    doing that to a finished ticket weakens a provenance line for no gain.

    For each remaining one - re-approve against the run branch, spawn the
    session, and on a promoted outcome merge its branch back. The merges are
    fast-forwards by construction, because each ticket branches from the run
    branch's then-current HEAD. Asserting `--ff-only` therefore costs nothing
    and buys an alarm: a non-fast-forward means something ran concurrently or a
    human intervened, and resolving it by hand would put code on the run branch
    that no `full_gate` ever judged.

    Root is left on the run branch. Returning it to `main` would hide the
    night's work from a continuation run, which finds the branch by looking at
    HEAD.
    """
    root = Path(root)
    resumed, run_branch = start_branch(root)

    try:
        tickets = {ticket_id: load_ticket(root, ticket_id) for ticket_id in ticket_ids}
        plan = plan_run(tickets, ticket_ids, done_set(root, ticket_ids, tickets))
    except BaseException:
        # Refusal, and the bare SystemExit(2) `load_ticket` raises through
        # `gate.die()`. Cut fresh there is no branch yet and nothing to roll
        # back; resumed, a night is already standing on the run branch and the
        # way to throw it away has to be on screen.
        if resumed:
            print_summary(run_branch, [], rollback_branches(root, run_branch), [])
        raise

    if not resumed:
        run_branch = new_run_branch()
        try:
            git(root, "checkout", "-q", "-b", run_branch, MAIN_BRANCH)
        except subprocess.CalledProcessError as e:
            raise Refusal(
                f"could not cut {run_branch} from {MAIN_BRANCH}: {(e.stderr or e.stdout).strip()}"
            ) from e

    results: list[tuple[str, str, str | None]] = [
        (ticket_id, "already done, skipped", None)
        for ticket_id in ticket_ids
        if ticket_id in plan.done
    ]
    # What this invocation created, checked rather than assumed. A session does
    # not create its branch and worktree before it does any work: `preflight`
    # runs first (edad/session.py:604, :607), so a ticket refused for a missing
    # blocker evidence record leaves neither - and in a queue that is the
    # likeliest refusal there is, because `blocked_by` is what a queue is for.
    # Naming them anyway aborts the discard command's `&&` chain on its first
    # step, so the rollback the operator watched scroll past deleted nothing and
    # the run branch is still standing. `rollback_branches` adds what earlier
    # invocations left on the same run branch.
    created: list[str] = []
    worktree_paths: list[str] = []
    code = 0

    try:
        for ticket_id in plan.order:
            reapprove(root, tickets[ticket_id])
            outcome = outcome_of(run_session(root, ticket_id))

            # Independently: a session can leave a branch with no worktree, or
            # a worktree without having promoted.
            branch = f"edad/{ticket_id.lower()}"
            if branch_exists(root, branch):
                created.append(branch)
            worktree = root / ".edad" / "worktrees" / ticket_id
            if worktree.resolve() in registered_worktrees(root):
                worktree_paths.append(str(worktree))

            if outcome != "promoted":
                results.append((ticket_id, outcome, None))
                code = 1
                continue

            merge = subprocess.run(
                ["git", "-C", str(root), "merge", "--ff-only", branch],
                capture_output=True,
                text=True,
                check=False,
            )
            if merge.returncode != 0:
                results.append((ticket_id, "promoted, merge refused", None))
                code = 1
                report_merge_refusal(branch, run_branch, merge)
                break

            results.append((ticket_id, outcome, git(root, "rev-parse", "HEAD")))
    finally:
        # Every exit path, including a mid-queue `Refusal` and the bare
        # SystemExit(2) that `load_ticket` raises through `gate.die()`. Making
        # approval a subprocess removed one instance of that hazard; it did not
        # remove the hazard.
        print_summary(
            run_branch, results, rollback_branches(root, run_branch, created), worktree_paths
        )

    return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="edad.session_queue")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a queue of approved tickets on one integration branch")
    r.add_argument("tickets", nargs="+", help="ticket ids, in the order they should run")
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run_queue(repo_root(), args.tickets)
    except Refusal as e:
        print(f"edad: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

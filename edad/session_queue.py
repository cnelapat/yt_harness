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
re-invoking the same command resumes. And T006's: D7 a failure is local, D8 two
breakers for the failures that are not about the tickets, D9 one run log, D10
one measured full gate on the run branch tip at the end. A bad night needs
nothing undone - the tip is verified after every merge, so an aborted ticket
leaves it where the last promotion did, and the only open question is what may
still run. `RunState` answers it and `run_queue` folds over that answer.

T009 amends three of those rather than patching them, because in all three the
implementation matched the ticket and the ticket was what was wrong. D23: the
final gate is bounded by `FINAL_GATE_TIMEOUT_S`, a quantity chosen for it, and
not by `command_timeout_s`, which bounds how long an agent may hang inside a
session. D24: D10's gate is the union of the `full_gate` commands of the tickets
*this run is made of*, not of every `.md` under `.edad/tickets/`. D25: the run
log's `stopped_because` answers "why did this stop" for every stop, not only for
D8's two - a queue halted by a merge git refused used to say so on stdout alone.

T013 makes the queue carry the sandbox tier. Every child was spawned with no
`--sandbox` and no `--network`, so the overnight run - the one path where nobody
is watching the agent - was the one path that never sandboxed. Now one
`--sandbox`/`--network` per night reaches every child (D1), the default stays
`none` (D4), `--sandbox` is stated in every child's argv even when it is the
default (D6), the run log names the tier at the top level (D4), and a docker
night ensures its proxy and validates its network once - after the plan and
before the branch is cut (D5), removing on the way out only what it created
(D17).

D13 is unenforced: v1 is sequential. `plan_run` computes `Plan.independent`
and nothing acts on it, so a later scheduler is a change rather than a
redesign - and so the wall-clock parallelism would have saved is measurable
rather than assumed.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Imported by name, not through their modules: the tests replace
# `ensure_egress_proxy`, `validate_network` and `remove_egress_proxy` *here*
# to observe the order a docker night reaches them in.
from edad.egress import EgressError, ensure_egress_proxy, remove_egress_proxy
from edad.gate import check_freeze, load_ticket, repo_root, run_commands
from edad.session import MAX_NO_PROGRESS, PROMOTED_OUTCOMES, Abort, validate_network

# D23. How long any one command of the final gate may run.
#
# This is NOT `command_timeout_s`. That is a `kill_conditions` entry and it
# bounds how long an *agent* may hang inside a session; the final gate is a
# suite the controller runs with no agent anywhere in it. Borrowing the agent's
# number means a ticket that raised its own - as T006 did, for measured reasons
# entirely about the agent - silently changes how long the night's last gate may
# take. Two quantities under one name is how a wall-clock budget stops meaning
# anything: the operator sets a bound, the queue honours it, and the epilogue
# then runs on a bound nobody chose.
#
# Fifteen minutes, chosen against what a `full_gate` is: a repo-wide test run
# and a linter, which take seconds here, so this is slack for a slow machine
# rather than an estimate. It is a constant rather than a flag because the
# quantity is a property of the gate, not of the night: an operator who wants a
# shorter night sets `--budget-hours`, which bounds the queue, and cutting the
# final gate short would forfeit the measurement instead of saving time.
FINAL_GATE_TIMEOUT_S = 900

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


def session_argv(
    ticket_id: str, sandbox: str = "none", network: str | None = None
) -> list[str]:
    """The command that runs one ticket. A subprocess, not an import.

    `gate.die()` raises a bare `SystemExit(2)` from twenty-five sites that
    `session.main()` does not catch, so one bad ticket in-process takes the
    whole night down. And a session holds the `edad.gate` it imported at start,
    which is what makes the verifier the pre-session code: a controller that
    imported once and looped would freeze that snapshot for the entire run, so
    the final state of the run branch would never have been judged by its own
    gate.

    `--sandbox` is stated in every branch, the default included (D6). Left
    off when it is `none`, the tier would be asserted by two files' defaults
    agreeing, and a running night's `ps` output would not say which tier it
    is. `--network` is appended exactly once, only when a name was given.
    """
    argv = [sys.executable, "-m", "edad.session", "run", ticket_id, "--sandbox", sandbox]
    if network is not None:
        argv += ["--network", network]
    return argv


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


def run_session(
    root: Path, ticket_id: str, sandbox: str = "none", network: str | None = None
) -> int:
    """Spawn one ticket's session and hand back its exit code."""
    return subprocess.run(
        session_argv(ticket_id, sandbox=sandbox, network=network), cwd=root, check=False
    ).returncode


# --- D7: a failure is local -------------------------------------------------


def skip_reason(failed_id: str) -> str:
    """Names the ticket that caused the skip, so the morning's triage is one
    line rather than a reconstruction of the dependency graph."""
    return f"blocked by {failed_id}, which failed"


# --- D8: breakers, for failures that are not about the tickets --------------


def no_commit_abort(session_log: dict) -> bool:
    """True when a session log shows a non-promoted outcome and no iteration
    that made a commit.

    Reads the log `edad.session` already writes - `asdict(SessionLog)`, so
    `outcome` against that module's own `PROMOTED_OUTCOMES` rather than a copy
    of it here, and `iterations[].made_commit`. An agent that exits non-zero and
    commits nothing is not failing the ticket, it is not running; an empty
    `iterations` is the strongest case of that, not an edge one.
    """
    if (session_log.get("outcome") or "") in PROMOTED_OUTCOMES:
        return False
    return not any(step.get("made_commit") for step in session_log.get("iterations") or [])


# STUB - T014 gives this its meaning: the logless breaker's threshold.
MAX_LOGLESS = MAX_NO_PROGRESS


def breaker_fired(  # STUB - T014 makes `logless` fire "no_log"
    *, no_commit_aborts: int, elapsed_s: float, budget_s: float | None = None,
    logless: int = 0,
) -> str | None:
    """`"no_progress"`, `"wall_clock"`, or `None`. Pure, keyword-only.

    `MAX_NO_PROGRESS` is the session's own threshold read one level up: inside a
    session it counts iterations that changed nothing, here it counts whole
    sessions that did. `budget_s=None` means no wall-clock bound. Wall-clock is
    the bound the operator agreed to when they went to bed; it does not bound
    the bill, and nothing here does. Keyword-only because all three arguments
    are numbers, and a positional call that transposed two would still run.
    """
    if no_commit_aborts >= MAX_NO_PROGRESS:
        return "no_progress"
    if budget_s is not None and elapsed_s > budget_s:
        return "wall_clock"
    return None


# --- D9: the run log --------------------------------------------------------


def log_entry(status: str, **fields) -> dict:
    """One ticket's line in the run log, every key present and null rather than
    absent - so a reader never has to tell "no merge sha because it never ran"
    from "no merge sha key in this shape of entry"."""
    entry = {
        "status": status,
        "merge_sha": None,
        "session_log": None,
        "skip_reason": None,
        "elapsed_s": None,
        "at_s": None,
    }
    entry.update(fields)
    return entry


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
    ran. Those tickets are in the log too, as `not_run` - being able to say
    which ones the night never got to is the other half of the same distinction.

    `stopped_because` (D25) is the wider question `breaker` only half answers.
    A queue stopped by a merge git refused logs the ticket `promoted, merge
    refused`, everything behind it `not_run`, and `breaker: null` - three true
    statements that never say why. It sits *alongside* `breaker` rather than
    replacing it, because a reader still needs to tell the two breakers from
    everything else; it is `None` on a clean drain, because a night that drained
    did not stop.

    `sandbox` and `network` are the night's tier (D4): one decision, carried
    here so `work_one` hands the same flags to every child, and written to the
    log at the top level so "did this night run sandboxed" is a lookup rather
    than a reconstruction from N session logs.
    """

    def __init__(
        self, plan: Plan, tickets: dict[str, dict], budget_s: float | None = None,
        sandbox: str = "none", network: str | None = None,
    ):
        self.plan = plan
        self.tickets = tickets
        self.budget_s = budget_s
        self.sandbox = sandbox
        self.network = network
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # monotonic, because the budget is a duration: a clock stepped by NTP
        # or by a DST change mid-night would otherwise fire the breaker, or
        # silently unbound it.
        self.started = time.monotonic()
        self.skipped: dict[str, str] = {}
        self.breaker: str | None = None
        self.stopped_because: str | None = None
        self.outcomes: dict[str, dict] = {}
        self.no_commit_aborts = 0
        self.logless = 0  # STUB - T014 counts and resets it in `fail` and `promote`
        self.final_gate: dict | None = None

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started

    def record(self, ticket_id: str, status: str, **fields) -> dict:
        """Create or update one ticket's entry, keeping what is already there:
        the caller writes the session log path and the timings down as soon as
        the session returns, and no later exit path has to re-attach them."""
        entry = self.outcomes.setdefault(ticket_id, log_entry(status))
        entry.update(status=status, at_s=round(self.elapsed_s(), 3), **fields)
        return entry

    def dependents(self, ticket_id: str) -> list[str]:
        """Everything in the queue transitively reachable from `ticket_id`
        through `blocked_by`. Reachability rather than the immediate edge: a
        ticket two hops away does not name the failure, and nothing it needs can
        exist without it either."""
        reached: set[str] = set()
        frontier = [ticket_id]
        while frontier:
            blocker = frontier.pop()
            for queued in self.plan.order:
                if queued == ticket_id or queued in reached:
                    continue
                if blocker in blockers_of(self.tickets, queued):
                    reached.add(queued)
                    frontier.append(queued)
        return [t for t in self.plan.order if t in reached]

    def stop(self, reason: str) -> None:
        """Why the night stopped, recorded once.

        First cause wins, for `breaker`'s reason: whatever actually stopped the
        queue is what the morning needs, and a later reading overwriting it
        renames the cause after the fact.
        """
        if self.stopped_because is None:
            self.stopped_because = reason

    def check_breaker(self) -> str | None:
        """The first breaker to fire wins and stands: re-deciding each time
        would let a later reading rename the reason the night stopped."""
        if self.breaker is None:
            self.breaker = breaker_fired(
                no_commit_aborts=self.no_commit_aborts,
                elapsed_s=self.elapsed_s(),
                budget_s=self.budget_s,
            )
            if self.breaker is not None:
                self.stop(f"breaker {self.breaker} fired; the queue stopped")
        return self.breaker

    def promote(self, ticket_id: str, merge_sha: str) -> None:
        self.no_commit_aborts = 0
        self.record(ticket_id, "promoted", merge_sha=merge_sha)
        self.check_breaker()

    def fail(self, ticket_id: str, session_log: dict | None = None) -> None:
        """Mark it failed, skip what it blocks, and count it toward the breaker.

        `session_log=None` means the log could not be read, and it resets the
        count rather than raising it. The breaker exists to stop a night that is
        not running; firing it on the absence of evidence would stop a night on
        a guess.
        """
        self.record(ticket_id, "failed")
        if session_log is not None and no_commit_abort(session_log):
            self.no_commit_aborts += 1
        else:
            self.no_commit_aborts = 0
        for dependent in self.dependents(ticket_id):
            # First cause wins: a ticket already skipped keeps the failure that
            # actually stopped it, not whichever later one also reaches it.
            if dependent in self.outcomes:
                continue
            self.skipped[dependent] = skip_reason(ticket_id)
            self.record(dependent, "skipped", skip_reason=self.skipped[dependent])
        self.check_breaker()

    def remaining(self) -> list[str]:
        """Still to run: queued, and not yet promoted, failed or skipped."""
        return [t for t in self.plan.order if t not in self.outcomes]

    def as_log(self) -> dict:
        tickets = dict(self.outcomes)
        for ticket_id in sorted(self.plan.done):
            tickets.setdefault(ticket_id, log_entry("already done"))
        for ticket_id in self.remaining():
            tickets.setdefault(ticket_id, log_entry("not_run"))
        return {
            "started_at": self.started_at,
            "elapsed_s": round(self.elapsed_s(), 3),
            "sandbox": self.sandbox,
            "network": self.network,
            "tickets": tickets,
            "breaker": self.breaker,
            "stopped_because": self.stopped_because,
            "final_gate": self.final_gate,
        }


def session_logs(root: Path, ticket_id: str) -> set[Path]:
    """The session logs on disk for one ticket, as paths."""
    return set((Path(root) / ".edad" / "sessions").glob(f"{ticket_id}-*.json"))


def read_new_log(before: set[Path], after: set[Path]) -> tuple[str | None, dict | None]:
    """The log this session just wrote, and its contents.

    Identified by difference rather than by "the newest one", because a resumed
    run re-running a ticket that already has logs would otherwise read an
    earlier night's - and an old aborted log counted as this session's is a
    false no-commit abort, which is half of a breaker.
    """
    written = sorted(after - before)
    if not written:
        return None, None
    path = written[-1]
    try:
        return str(path), json.loads(path.read_text())
    except (OSError, ValueError):
        return str(path), None


# --- the driver -------------------------------------------------------------


def merge_refusal_reason(branch: str, run: str) -> str:
    """The one sentence the screen and the run log both carry (D25).

    One expression rather than two, for the reason `evidence_path` is one: the
    operator correlating a printed refusal against `.edad/runs/` at 8am must not
    have to decide whether two differently-worded lines are the same event.
    Git's own diagnosis stays on screen only - it is many lines and it is
    already captured in the session's own output - and what the log gets is
    which merge refused.
    """
    return f"{branch} would not fast-forward onto {run}"


def report_merge_refusal(branch: str, run: str, merge: subprocess.CompletedProcess) -> None:
    """Git's own words, verbatim.

    Git already answered the question this controller cannot: `--ff-only` fails
    on divergence and on a dirty root tree alike, only git knows which, and
    only git names the blocking path. A fixed "something ran concurrently"
    sends a half-awake operator hunting a concurrent run that never happened,
    while the real cause - one uncommitted edit - is the one thing not on
    screen.
    """
    print(f"\n{merge_refusal_reason(branch, run)}. git said:")
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
    tickets: dict[str, dict],
    ticket_branches: Sequence[str],
    worktree_paths: Sequence[str],
) -> None:
    """The summary and the rollback, printed on every exit path that has a run
    branch - a clean finish, a merge git refused, a `Refusal` raised between two
    tickets, and a plan-stage `Refusal` on a *resumed* run alike, because
    whatever already merged is sitting on the run branch in all four.

    Not on a plan-stage refusal from a fresh cut: the plan refuses before the
    branch exists, so a discard command there names a branch that was never
    created, `git branch -D` fails, and the operator watches a rollback that
    could not have worked. The discard command goes last, so it is the final
    runnable line. `tickets` is the run log's own per-ticket mapping, so the
    screen and the file cannot disagree about what happened to any one of them."""
    print(f"\nrun branch: {run_branch}")
    if not tickets:
        print("  (no ticket ran)")
    for ticket_id, entry in tickets.items():
        line = f"  {ticket_id}: {entry['status']}"
        if entry["merge_sha"]:
            line += f", merged {entry['merge_sha'][:12]}"
        if entry["skip_reason"]:
            line += f" ({entry['skip_reason']})"
        print(line)
    print("\nto discard this run entirely:")
    print("  " + discard_command(run_branch, ticket_branches, worktree_paths))


# --- D10: one measured claim at the end -------------------------------------


def final_gate_spec(root: Path, ticket_ids: Sequence[str]) -> tuple[list[str], int]:
    """What a repo-wide full gate is for *this run*, and how long a command gets.

    Taken from the tickets rather than from a second list, for the reason
    ordering is taken from `blocked_by`: a gate definition kept beside the one
    the tickets already carry is a place for the two to disagree. `full_gate`
    runs repo-wide by construction, so every ticket's list is a claim about the
    same tree, and the union is every command any of them counts as the gate.

    D24: "the tickets" means the ones the run is made of, not every `.md` under
    `.edad/tickets/`. Today all of them declare the same two commands, so the
    difference is invisible; the first ticket to declare a third would otherwise
    join the final gate of every run that does not include it, and the night
    would end measuring a claim about code it never touched.

    First-seen order across `ticket_ids`, deduplicated - so the gate reads in
    the order the queue ran, and asking for the same run twice cannot reorder it.
    The ids are a parameter rather than something read off a `RunState`, which
    keeps this as testable as it was when it globbed.
    """
    commands: list[str] = []
    for ticket_id in ticket_ids:
        for command in load_ticket(root, ticket_id).get("full_gate") or []:
            if command not in commands:
                commands.append(command)
    return commands, FINAL_GATE_TIMEOUT_S


def final_gate(root: Path, ticket_ids: Sequence[str]) -> dict:
    """One full gate on the run branch tip, after the queue drains.

    Redundant by derivation from the fast-forward property - ticket N's gate
    already ran against every earlier ticket's merged code - and it runs anyway,
    because this harness holds that a measured claim beats a derived one. If it
    ever fails while every ticket promoted, the derivation is wrong somewhere;
    the run log reports it and nothing else is designed.

    No ratchet and no baseline: those answer "did this ticket break something",
    against one ticket's approval lock. This asks the plainer question the night
    ends on - is the tree the operator will read in the morning green.
    """
    commands, timeout_s = final_gate_spec(root, ticket_ids)
    results = run_commands(root, commands, deny_network=True, timeout_s=timeout_s)
    return {
        "passed": all(c.ok for c in results),
        "commands": [
            {
                "command": c.command,
                "exit_code": c.exit_code,
                "duration_s": c.duration_s,
                "timed_out": c.timed_out,
                "output_tail": c.output_tail,
            }
            for c in results
        ],
    }


def report_final_gate(gate: dict) -> None:
    """Loudly, and to stderr. By D10's derivation this cannot happen, so if it
    did the derivation is wrong somewhere and no automated response would be
    trustworthy - reporting it is the whole designed reaction."""
    if gate.get("passed"):
        return
    print(
        "\nthe final full gate FAILED on the run branch tip. Every promoted ticket "
        "passed a full gate against this same code, so this should have been "
        "impossible: something the harness derives is wrong. Nothing is undone "
        "automatically - the run log has the commands and their output.",
        file=sys.stderr,
    )
    for command in gate.get("commands") or []:
        if command["exit_code"] != 0:
            print(f"  failed ({command['exit_code']}): {command['command']}", file=sys.stderr)


def write_run_log(root: Path, run_branch: str, state: RunState) -> Path:
    """One file the morning can be read from.

    Telemetry, so `.edad/runs/` is gitignored beside `.edad/records/` and
    `.edad/sessions/`. Committing it would be worse than untidy: the run branch
    is what the operator reviews, and a log of the night in that diff is a file
    no ticket's scope allows.
    """
    directory = Path(root) / ".edad" / "runs"
    directory.mkdir(parents=True, exist_ok=True)
    payload = state.as_log()
    payload["run_branch"] = run_branch
    stamp = state.started_at.replace(":", "").replace("-", "")[:15]
    path = directory / f"{stamp}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def exit_code(state: RunState) -> int:
    """0 only if every queued ticket promoted or was already done, no breaker
    fired, and the final gate passed. A breaker is a non-zero exit even though
    no ticket failed: the night did not do what it was asked."""
    settled = {"promoted", "already done"}
    if any(e["status"] not in settled for e in state.as_log()["tickets"].values()):
        return 1
    if state.breaker is not None:
        return 1
    return 0 if state.final_gate is None or state.final_gate.get("passed") else 1


@dataclass
class Created:
    """What this invocation made, and so what the rollback must name.

    Checked rather than assumed: `preflight` runs before a session creates its
    branch and worktree (`edad/session.py:604`, `:607`), so a ticket refused for
    a missing blocker record leaves neither - the likeliest refusal in a queue,
    since `blocked_by` is what a queue is for. Naming them anyway aborts the
    discard command's `&&` chain on its first step, and the rollback the
    operator watched scroll past deleted nothing at all.
    """

    branches: list[str] = field(default_factory=list)
    worktrees: list[str] = field(default_factory=list)


def work_one(root: Path, ticket_id: str, state: RunState, created: Created, run: str) -> bool:
    """Re-approve, run, record, merge. False means the run stops here.

    A ticket that fails returns True: the failure is local (D7), `state.fail`
    has already skipped everything that needed it, and whatever is independent
    of it still deserves the night. Only a merge git refused stops the queue -
    from there the run branch is in a state nothing here may resolve without
    putting unjudged code on it.
    """
    reapprove(root, state.tickets[ticket_id])
    before = session_logs(root, ticket_id)
    started = time.monotonic()
    # The tier the state carries, always - default included. Passing it "only
    # when it is not the default" is the drift D6 exists to close.
    outcome = outcome_of(run_session(root, ticket_id, state.sandbox, state.network))
    log_path, session_log = read_new_log(before, session_logs(root, ticket_id))
    state.record(
        ticket_id, outcome, session_log=log_path, elapsed_s=round(time.monotonic() - started, 3)
    )

    # Independently: a session can leave a branch with no worktree, or a
    # worktree without having promoted.
    branch = f"edad/{ticket_id.lower()}"
    if branch_exists(root, branch):
        created.branches.append(branch)
    worktree = root / ".edad" / "worktrees" / ticket_id
    if worktree.resolve() in registered_worktrees(root):
        created.worktrees.append(str(worktree))

    if outcome != "promoted":
        state.fail(ticket_id, session_log=session_log)
        return True

    merge = subprocess.run(
        ["git", "-C", str(root), "merge", "--ff-only", branch],
        capture_output=True,
        text=True,
        check=False,
    )
    if merge.returncode != 0:
        state.record(ticket_id, "promoted, merge refused")
        # Recorded where it is reported, and from the same expression, so the
        # file and the screen cannot end up carrying different sentences about
        # the same refusal. No breaker fires here - this is not one of D8's two
        # - which is exactly why `breaker` alone left the run log unable to say
        # why the queue stopped.
        state.stop(merge_refusal_reason(branch, run))
        report_merge_refusal(branch, run, merge)
        return False
    state.promote(ticket_id, git(root, "rev-parse", "HEAD"))
    return True


def work_queue(root: Path, state: RunState, created: Created, run_branch: str) -> None:
    """The queue, ticket by ticket, until it drains or a breaker fires.

    The breaker is checked before each ticket rather than after: firing it after
    spawning the session it was meant to prevent spends exactly the hour the
    budget existed to save.
    """
    for ticket_id in state.plan.order:
        if ticket_id in state.skipped:
            continue
        if state.check_breaker() is not None:
            break
        if not work_one(root, ticket_id, state, created, run_branch):
            break
    state.final_gate = final_gate(root, state.plan.order)
    report_final_gate(state.final_gate)


def prepare_tier(sandbox: str, network: str | None) -> bool:
    """Ensure the proxy and validate the network, once per night. True if this
    run created the proxy and so must remove it (D17).

    Called between the plan and the branch cut (D5): a misnamed network costs
    seconds at ticket 0 rather than a night of instant aborts that the run log
    reports as "ran out of tickets". `ensure` comes first because
    `validate_network`'s permit probe goes through the proxy; a docker night
    with no network never reaches `ensure`, and `validate_network` refuses it.
    A `none` night asks docker nothing.

    `Abort` and `EgressError` become `Refusal`, so `main()` prints and exits 2
    as for every other refusal. A refused validation is the run's first exit,
    and the `finally` that tears the proxy down does not exist yet - so what
    `ensure` just made is removed here, or a refused docker night leaks it.
    """
    if sandbox != "docker":
        return False
    created = False
    try:
        if network is not None:
            created = ensure_egress_proxy(network)
        validate_network(sandbox, network)
    except (Abort, EgressError) as e:
        if created:
            remove_egress_proxy(network)
        raise Refusal(str(e)) from e
    return created


def run_queue(
    root: Path, ticket_ids: list[str], budget_s: float | None = None,
    sandbox: str = "none", network: str | None = None,
) -> int:
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

    A ticket that fails takes down only what depended on it (D7); two breakers
    stop the whole queue for reasons that are not about the tickets at all (D8);
    the night is written to one file (D9) and ends with one measured full gate
    on the run branch tip (D10).

    Root is left on the run branch. Returning it to `main` would hide the
    night's work from a continuation run, which finds the branch by looking at
    HEAD.
    """
    root = Path(root)
    resumed, run_branch = start_branch(root)

    try:
        tickets = {ticket_id: load_ticket(root, ticket_id) for ticket_id in ticket_ids}
        plan = plan_run(tickets, ticket_ids, done_set(root, ticket_ids, tickets))
        # After the plan, before the branch: a docker night's proxy and network
        # are validated once, with root still on `main` and no session spawned.
        created_proxy = prepare_tier(sandbox, network)
    except BaseException:
        # Refusal, and the bare SystemExit(2) `load_ticket` raises through
        # `gate.die()`. Cut fresh there is no branch yet and nothing to roll
        # back; resumed, a night is already standing on the run branch and the
        # way to throw it away has to be on screen.
        if resumed:
            print_summary(run_branch, {}, rollback_branches(root, run_branch), [])
        raise

    if not resumed:
        run_branch = new_run_branch()
        try:
            git(root, "checkout", "-q", "-b", run_branch, MAIN_BRANCH)
        except subprocess.CalledProcessError as e:
            if created_proxy:
                remove_egress_proxy(network)
            raise Refusal(
                f"could not cut {run_branch} from {MAIN_BRANCH}: {(e.stderr or e.stdout).strip()}"
            ) from e

    state = RunState(plan, tickets, budget_s=budget_s, sandbox=sandbox, network=network)
    created = Created()

    try:
        work_queue(root, state, created, run_branch)
    finally:
        # Every exit path, including a mid-queue `Refusal` and the bare
        # SystemExit(2) that `load_ticket` raises through `gate.die()`. Making
        # approval a subprocess removed one instance of that hazard; it did not
        # remove the hazard. `rollback_branches` adds what earlier invocations
        # left on the same run branch, which is what makes a resumed run's
        # discard cover the whole night rather than this invocation's part.
        #
        # The proxy goes on the same path, and only if this run made it (D17):
        # one already present when the night started - an operator's, or a
        # concurrent run's - is not this run's to remove.
        if created_proxy:
            remove_egress_proxy(network)
        if state.breaker is not None:
            print(f"\nbreaker: {state.breaker}. The queue stopped; the tickets it never")
            print("reached did not fail and are logged as not_run.")
        print(f"\nrun log: {write_run_log(root, run_branch, state)}")
        print_summary(
            run_branch,
            state.as_log()["tickets"],
            rollback_branches(root, run_branch, created.branches),
            created.worktrees,
        )

    return exit_code(state)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="edad.session_queue")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a queue of approved tickets on one integration branch")
    r.add_argument("tickets", nargs="+", help="ticket ids, in the order they should run")
    # The session's own flags, choices and defaults included (D1, D4): one
    # decision per night, uniform across the queue, and a night nobody thought
    # about changes nothing. `--image` and `--yolo` are deferred.
    r.add_argument(
        "--sandbox",
        choices=["none", "docker"],
        default="none",
        help="run every child's agent under this tier; validated once, at plan time",
    )
    r.add_argument(
        "--network",
        metavar="NAME",
        default=None,
        help="attach every agent container to this docker network instead of "
        "--network none; refused unless docker reports it internal. The egress "
        "proxy is ensured once for the night.",
    )
    r.add_argument(
        "--budget-hours",
        type=float,
        default=None,
        help="stop the queue once this much wall clock has passed. Bounds the "
        "night, not the bill - nothing here bounds the bill. Omitted, the queue "
        "runs until it drains.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    budget_s = None if args.budget_hours is None else args.budget_hours * 3600
    try:
        return run_queue(
            repo_root(), args.tickets, budget_s=budget_s,
            sandbox=args.sandbox, network=args.network,
        )
    except Refusal as e:
        print(f"edad: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

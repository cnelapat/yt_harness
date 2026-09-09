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
leaves you. T005's (D1, D6, D12) and T006's (D7, D8, D9, D10) belong to their
own tickets; drafts of both are recoverable from commit `1a48d51`.

D13 is unenforced: v1 is sequential. Nothing below is built against parallel
execution, and nothing below hardcodes against it either, so a later scheduler
is a change rather than a redesign.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from edad.gate import check_freeze, load_ticket, repo_root

MAIN_BRANCH = "main"


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
    return "edad/run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


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
    """The summary and the rollback, printed on every exit path - a clean
    finish, a merge git refused, and a `Refusal` raised between two tickets
    alike, because whatever already merged is sitting on the run branch in all
    three. The discard command goes last, so it is the final runnable line."""
    print(f"\nrun branch: {run_branch}")
    if not results:
        print("  (no ticket ran)")
    for ticket_id, outcome, sha in results:
        print(f"  {ticket_id}: {outcome}" + (f", merged {sha[:12]}" if sha else ""))
    print("\nto discard this run entirely:")
    print("  " + discard_command(run_branch, ticket_branches, worktree_paths))


def run_queue(root: Path, ticket_ids: list[str]) -> int:
    """Cut an integration branch from `main` and work the queue on it.

    Ids run in the order given; ordering, cycles and unsatisfiable blockers are
    T005's. For each one - re-approve against the run branch, spawn the
    session, and on a promoted outcome merge its branch back.

    The merges are fast-forwards by construction, because each ticket branches
    from the run branch's then-current HEAD. Asserting `--ff-only` therefore
    costs nothing and buys an alarm: a non-fast-forward means something ran
    concurrently or a human intervened, and resolving it by hand would put code
    on the run branch that no `full_gate` ever judged.

    Root is left on the run branch. Returning it to `main` would hide the
    night's work from a continuation run, which finds the branch by looking at
    HEAD.
    """
    root = Path(root)
    run_branch = new_run_branch()
    try:
        git(root, "checkout", "-q", "-b", run_branch, MAIN_BRANCH)
    except subprocess.CalledProcessError as e:
        raise Refusal(
            f"could not cut {run_branch} from {MAIN_BRANCH}: {(e.stderr or e.stdout).strip()}"
        ) from e

    results: list[tuple[str, str, str | None]] = []
    # What the run actually created, checked rather than assumed. A session
    # does not create its branch and worktree before it does any work:
    # `preflight` runs first (edad/session.py:604, :607), so a ticket refused
    # for a missing blocker evidence record leaves neither - and in a queue
    # that is the likeliest refusal there is, because `blocked_by` is what a
    # queue is for. Naming them anyway aborts the discard command's `&&` chain
    # on its first step, so the rollback the operator watched scroll past
    # deleted nothing and the run branch is still standing.
    ticket_branches: list[str] = []
    worktree_paths: list[str] = []
    code = 0

    try:
        for ticket_id in ticket_ids:
            ticket = load_ticket(root, ticket_id)
            reapprove(root, ticket)
            outcome = outcome_of(run_session(root, ticket_id))

            # Independently: a session can leave a branch with no worktree, or
            # a worktree without having promoted.
            branch = f"edad/{ticket_id.lower()}"
            if branch_exists(root, branch):
                ticket_branches.append(branch)
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
        print_summary(run_branch, results, ticket_branches, worktree_paths)

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

"""
EDAD session controller.

Runs one ticket unattended. The loop terminates on the gate's exit code, never
on the agent's claim to be finished — an agent cannot produce a passing verdict
except by satisfying acceptance tests it is not permitted to edit.

  worktree  ->  [ prompt -> agent -> commit -> gate ]xN  ->  full gate  ->  evidence

Nothing is merged to a mainline branch. A finished session leaves a branch and
a signed-off record; the merge decision stays with a human.

Usage
    python3 -m edad.session run T001                    # local worktree
    python3 -m edad.session run T001 --sandbox docker   # network-isolated
    python3 -m edad.session run T001 --sandbox docker --network edad-fixtures
                                                       # ... plus that network,
                                                       # if docker calls it internal
    python3 -m edad.session run T001 --dry-run          # prompt only, no agent
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from edad.gate import (
    Record,
    changed_files,
    check_freeze,
    evaluate,
    frozen_blocks,
    gate_toolchain_problems,
    git,
    load_ticket,
    repo_root,
    write_record,
)

DEFAULT_IMAGE = "edad-agent:latest"
AGENT_TIMEOUT_S = 900
AGENT_TAIL = 2000
# An agent that exits non-zero and commits nothing is not failing the ticket,
# it is not running. One retry absorbs a transient; two in a row is systematic.
MAX_NO_PROGRESS = 2


# --- preflight -------------------------------------------------------------


class Abort(Exception):
    """Stop the session. Carries the reason recorded in the session log."""


class Unwinnable(Abort):
    """The ticket could never have passed: full_gate fails inside a frozen file
    no permitted edit can reach.

    A subclass so every existing handler still stops the session, but a distinct
    outcome in the log, because it answers a different question. "aborted" means
    the agent could not do the work; "unwinnable" means the work was impossible
    as specified - a defect in Prepare, not a failure in Build. Across a run of
    sessions that is the count worth having: how often the ticket-writing, not
    the agent, was the problem.
    """


def preflight(root: Path, ticket: dict, sandbox: str, dry_run: bool,
              network: str | None = None) -> None:
    """Refuse to start rather than fail expensively halfway through."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise Abort(
            "ANTHROPIC_API_KEY is set. An unattended loop with a key present bills "
            "the API account instead of the subscription. Unset it and re-run."
        )
    tools = gate_toolchain_problems(root)
    if tools:
        raise Abort(
            "the gate's toolchain does not match requirements-gate.txt: "
            + "; ".join(tools)
            + ". A gate run on the wrong toolchain reports FAIL for the toolchain "
            "rather than the code, and feeds that to the agent as evidence. "
            "Install the pins: pip install -r requirements-gate.txt"
        )
    # Approval is the lock, not a field. A ticket asserting `status: approved`
    # was the ticket vouching for itself: it added a way to be wrong (a hand-set
    # field on an unapproved ticket) and no way to be right, since nothing could
    # be concluded from it that the lock did not already prove. What approval
    # actually means is what these two lines check - a lock exists, and its
    # hashes still match the tree.
    if not (root / ".edad" / "hashes" / f"{ticket['id']}.json").exists():
        raise Abort("no approval lock; run 'python3 -m edad.gate approve <ticket>'")

    ok, problems = check_freeze(root, ticket)
    if not ok:
        raise Abort("frozen files already differ from the approval lock: " + "; ".join(problems))

    blocked = ticket.get("blocked_by") or []
    unmet = [b for b in blocked if not (root / ".edad" / "evidence" / f"{b}.json").exists()]
    if unmet:
        # The refusal is right and must stay: this worktree branches from the
        # current HEAD, so an unmerged blocker's CODE is not in it either, and
        # the ticket genuinely cannot proceed. Only the wording was wrong.
        # "no evidence" reads as "that ticket has not been done", which sends
        # the reader off to re-run a session that already passed. What is
        # actually missing is the merge.
        branches = ", ".join(f"edad/{b.lower()}" for b in unmet)
        raise Abort(
            f"no evidence record here for: {', '.join(unmet)}. That does not mean "
            f"they failed. A passing session commits its evidence on the ticket's "
            f"OWN branch ({branches}) and merges nothing, so the record only "
            f"becomes visible at {root / '.edad' / 'evidence'} once you merge. "
            f"Check whether those branches passed and need merging; if no session "
            f"has run them, run those first. Do not copy the record across by "
            f"hand - merging is also what puts the blocker's code into this "
            f"worktree, which is what this ticket is actually waiting for."
        )

    # changed_files() filters .edad/ — the harness's own logs and worktrees
    # must not count as user changes, or a session can never run twice.
    dirty = changed_files(root, None)
    if dirty:
        raise Abort("working tree is dirty; commit or stash first: " + ", ".join(dirty[:5]))
    if not dry_run:
        if not shutil.which("claude"):
            raise Abort("the 'claude' CLI is not on PATH")
        if agent_has_credential() is False:
            raise Abort(
                "the 'claude' CLI reports it is not logged in. Run 'claude setup-token' "
                "and export CLAUDE_CODE_OAUTH_TOKEN, or 'claude auth login'."
            )
        if sandbox == "docker" and not shutil.which("docker"):
            raise Abort("--sandbox docker requested but docker is not on PATH")

    # Last, so the plainer refusals above (no docker at all) speak first: this
    # one's message is about a network, and "cannot report on it" is a poor way
    # to say docker is not installed.
    validate_network(sandbox, network)


def docker_network_internal(name: str) -> str | None:
    """Docker's own answer to whether `name` is an internal network: the
    stripped `{{.Internal}}` output, or None when docker did not answer.

    Every way of not answering collapses to None on purpose - no such network,
    daemon not running, docker not installed. The caller does not act on the
    distinction: what it needs to know is whether the isolation was measured,
    and an unmeasured network is refused whatever the reason.

    The only thing in this tier that shells out, and a module-level name so the
    refusal logic can be tested against each answer without a daemon.
    """
    try:
        proc = subprocess.run(
            ["docker", "network", "inspect", name, "--format", "{{.Internal}}"],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def validate_network(sandbox: str, network: str | None) -> None:
    """Refuse a --network the harness cannot back with docker's own answer.

    The default tier asserts nothing, so it asks nothing: probing on every run
    would let an unrelated docker problem refuse sessions that never needed
    docker. Once a name is given the prompt is going to tell the agent it has
    no internet egress, and network_rule's rule applies - the claim is only
    allowed because this measured it. An 'isolated' network with a gateway
    answers "false" and is refused here rather than lied about later.
    """
    if network is None:
        return
    if sandbox != "docker":
        raise Abort(
            f"--network {network} needs --sandbox docker: the network attaches a "
            "container, and an unsandboxed run has no container to attach."
        )
    internal = docker_network_internal(network)
    if internal is None:
        raise Abort(
            f"docker could not report on network {network!r} (no such network, no "
            "daemon, or no docker). Its isolation was not measured, so it will not "
            "be promised to the agent. Create it with: "
            f"docker network create --internal {network}"
        )
    if internal != "true":
        raise Abort(
            f"network {network!r} exists but is not internal (docker reports "
            f"Internal={internal!r}), so it reaches the internet. Attaching to it "
            "would put a 'no network access' claim in every prompt that nothing "
            "enforces. Re-create it with: "
            f"docker network create --internal {network}"
        )


def agent_has_credential() -> bool | None:
    """Whether the CLI has *a* credential: an exported token or a keychain login.
    True yes, False no, None the CLI could not answer.

    Requiring CLAUDE_CODE_OAUTH_TOKEN was wrong - a keychain login runs the
    agent fine, so that check refused sessions that would have worked. This is
    deliberately not a proof that the credential is *valid*: `claude auth
    status` reports loggedIn:true for a malformed token too, so a 401 still
    reaches the loop. MAX_NO_PROGRESS is what catches that.

    The None case matters as much as the False one. `claude auth status` is a
    real subcommand today and prints JSON with loggedIn, but it is a CLI
    surface we do not control: a release that renames it, drops it, or stops
    emitting JSON would turn a preflight convenience into a hard refusal of
    every session, for a credential that works. So an unrecognized command or
    unparseable output is "unknown, proceed" - the loop runs, and if the
    credential really is missing the agent exits non-zero committing nothing
    and MAX_NO_PROGRESS aborts with the agent's own error. Only an explicit
    loggedIn:false, which is unambiguous, refuses up front.
    """
    try:
        proc = subprocess.run(
            ["claude", "auth", "status"], capture_output=True, text=True,
            check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "loggedIn" not in payload:
        return None
    return bool(payload["loggedIn"])

# --- worktree --------------------------------------------------------------


def make_worktree(root: Path, ticket_id: str, base: str) -> tuple[Path, str]:
    """Isolated checkout on its own branch. The agent never sees the mainline."""
    wt = root / ".edad" / "worktrees" / ticket_id
    branch = f"edad/{ticket_id.lower()}"
    if wt.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=root, check=False)
    subprocess.run(["git", "branch", "-D", branch], cwd=root, check=False,
                   capture_output=True)
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "-b", branch, str(wt), base],
                   cwd=root, check=True, capture_output=True)
    return wt, branch


# --- prompting -------------------------------------------------------------


def network_rule(sandbox: str, cont_indent: str = "", network: str | None = None) -> str:
    """The network sentence, matched to the run it is in.

    Under --sandbox none the agent does have the network, and asserting
    otherwise puts an unenforceable claim in a prompt whose other rules are all
    mechanically checked. Shared by both prompts: the retry prompt restates the
    constraints, so a hardcoded "no network" there reintroduces on iteration 2
    exactly the lie the initial prompt is careful not to tell on iteration 1.

    The named tier is the same rule applied a third time. The container is on a
    network, so "--network none" would be false; it still cannot leave that
    network, because validate_network refused to start unless docker called it
    internal. Both halves are said, because an agent told only "no internet"
    will not think to reach the service it was given.
    """
    if sandbox == "docker" and network is not None:
        lines = [
            f"You are attached to the docker network {network}: services on it are",
            "reachable by container name (for example a database at its container",
            "name, on its own port). You have no internet egress - the network is",
            "internal, and this was confirmed with docker before the run started.",
            "Do not attempt installs or downloads.",
        ]
    elif sandbox == "docker":
        lines = [
            "You have no network access: the container runs with --network none.",
            "Do not attempt installs or downloads.",
        ]
    else:
        lines = [
            "Do not use the network: no installs, no downloads. The pinned",
            "toolchain is already present and complete. This run is unsandboxed,",
            "so unlike the other rules here this one is not mechanically enforced.",
            "It is still a requirement.",
        ]
    return ("\n" + cont_indent).join(lines)


def initial_prompt(ticket: dict, sandbox: str = "none", network: str | None = None) -> str:
    scope = "\n".join(f"  - {s}" for s in ticket.get("scope") or [])
    frozen = "\n".join(f"  - {s}" for s in ticket.get("frozen") or [])
    accept = "\n".join(f"  {c}" for c in ticket.get("acceptance") or [])
    network_item = f"4. {network_rule(sandbox, cont_indent='   ', network=network)}"
    return f"""You are implementing ticket {ticket['id']}: {ticket.get('title', '')}

{ticket.get('_body', '')}

RULES — these are enforced mechanically after every iteration. Violating any of
them ends the session immediately and discards the work.

1. You may create or modify ONLY these files:
{scope}

2. These files are FROZEN. Read them. Do not edit them, delete them, or add
   files that shadow them. Their hashes are checked before the tests run, and a
   mismatch means the tests are not run at all:
{frozen}

3. You are done when these commands exit 0 — not when you believe the work is
   complete. Your own assessment is not consulted. Run them yourself as you go;
   the same commands decide the verdict:
{accept}

{network_item}

Work directly in the repository. Do not create a summary, a report, or a
completion file; nothing you write about your work is read.
"""


def retry_prompt(ticket: dict, rec: Record, iteration: int, sandbox: str = "none",
                 network: str | None = None) -> str:
    # A killed command is described as killed. Presenting a timeout as "exit
    # 124" alongside real failures sends the agent to debug an assertion that
    # never ran; what it needs to know is that something did not terminate.
    def describe(c) -> str:
        if c.timed_out:
            return (
                f"$ {c.command}\nKILLED after {c.duration_s}s - this command did not "
                f"finish and reported no result. Something is not terminating. The "
                f"output below is partial:\n{c.output_tail[-1500:]}"
            )
        return f"$ {c.command}\nexit {c.exit_code}\n{c.output_tail[-1500:]}"

    fails = "\n\n".join(describe(c) for c in rec.commands if not c.ok)
    return f"""Iteration {iteration} of ticket {ticket['id']} did not pass the gate.

This is the verifier's own output, not a summary:

{fails}

Fix the implementation. The same constraints apply: only the files in scope,
and the frozen tests stay untouched.

{network_rule(sandbox, network=network)}
"""


# --- agent invocation ------------------------------------------------------


def agent_argv(  # noqa: PLR0913  # a pure argv builder: six independent inputs, not six jobs
    prompt: str, workdir: Path, sandbox: str, image: str, yolo: bool,
    network: str | None = None,
) -> list[str]:
    """Build the agent command.

    Flags differ across Claude Code releases — verify with `claude --help` and
    override with EDAD_AGENT_CMD if yours differs. The invocation is pinned
    explicitly rather than relying on defaults, because `-p` defaults are
    documented as changing in future releases.

    `network` names a docker network to attach instead of the default isolation;
    validate_network has already confirmed with docker that it is internal. It
    is keyword-defaulted last so every existing positional call is unchanged,
    and exactly one --network is emitted either way: docker accepts the flag
    twice and silently keeps one, so a second would not be a stricter run.
    """
    override = os.environ.get("EDAD_AGENT_CMD")
    base = shlex.split(override) if override else ["claude", "-p"]
    perm = ["--dangerously-skip-permissions"] if yolo else ["--permission-mode", "acceptEdits"]
    inner = [*base, *perm, prompt]

    if sandbox != "docker":
        return inner
    return [
        "docker", "run", "--rm",
        # No name: the deny in kill_conditions, made real. A name: a network
        # docker itself called internal, which is the same deny one tier wider.
        "--network", network if network is not None else "none",
        "-v", f"{workdir}:/work",
        "-w", "/work",
        "-e", "CLAUDE_CODE_OAUTH_TOKEN",
        image,
        *inner,
    ]


def run_agent(argv: list[str], workdir: Path) -> tuple[int, str]:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        proc = subprocess.run(
            argv, cwd=workdir, capture_output=True, text=True,
            timeout=AGENT_TIMEOUT_S, env=env, check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"agent exceeded {AGENT_TIMEOUT_S}s and was killed"
    return proc.returncode, (proc.stdout + proc.stderr)[-4000:]


# --- kill conditions -------------------------------------------------------


def diff_line_count(wt: Path, base: str) -> int:
    out = git(wt, "diff", "--numstat", base, "HEAD")
    total = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            add, rem = parts[0], parts[1]
            total += (int(add) if add.isdigit() else 0) + (int(rem) if rem.isdigit() else 0)
    return total


def failure_signature(rec: Record) -> str:
    """Stable fingerprint of *what* failed, so repeated identical failure is
    detectable. Prefers pytest node ids; falls back to hashing the output."""
    # A timeout first, and deliberately not hashed. The partial output of a hang
    # varies run to run, so hashing it makes every hang a fresh signature and
    # same_test_fails_consecutively never fires - the session burns its whole
    # iteration budget at full timeout each time. Keyed on the command alone,
    # a repeated hang is repeated identical failure, which is what it is.
    killed = sorted(c.command for c in rec.commands if c.timed_out)
    if killed:
        return "timeout:" + "|".join(killed)

    ids: list[str] = []
    for c in rec.commands:
        if c.ok:
            continue
        ids += re.findall(r"^(?:FAILED|ERROR) (\S+)", c.output_tail, re.MULTILINE)
    if ids:
        return "|".join(sorted(set(ids)))
    blob = "".join(c.output_tail for c in rec.commands if not c.ok)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def check_kills(ticket: dict, rec: Record, wt: Path, base: str,
                signatures: list[str]) -> str | None:
    k = ticket.get("kill_conditions") or {}
    if k.get("frozen_file_hash_mismatch", True) and not rec.freeze_ok:
        return "frozen acceptance test was modified"
    if rec.scope_enforced and not rec.scope_ok:
        # Name them. The session already holds the list, and without it the
        # reader learns only that SOME file was out of scope and has to
        # reconstruct which from the diff. With it, widening the ticket is one
        # edit and one re-approve. Scope stays strict; only the message widens.
        strays = [v.removeprefix("out of scope: ") for v in rec.scope_violations]
        return (
            "diff touched files outside the ticket's scope: "
            + ", ".join(strays)
            + f". Declared scope: {', '.join(ticket.get('scope') or []) or '(none)'}. "
            "If the ticket should have covered these, add them to `scope` and "
            "re-approve; the freeze check will require it."
        )
    budget = k.get("max_diff_lines")
    if budget:
        n = diff_line_count(wt, base)
        if n > budget:
            return f"diff is {n} lines, budget is {budget}"
    repeat = k.get("same_test_fails_consecutively")
    if repeat and len(signatures) >= repeat:
        tail = signatures[-repeat:]
        if len(set(tail)) == 1 and tail[0]:
            return f"identical failure {repeat} iterations running: {tail[0][:120]}"
    return None


# --- session ---------------------------------------------------------------


@dataclass
class Iteration:
    n: int
    agent_exit: int
    commit: str
    gate_passed: bool
    signature: str
    violations: list[str] = field(default_factory=list)
    made_commit: bool = True
    # The agent's own stderr. Not evidence - the gate decides the verdict - but
    # without it an infrastructure failure (a 401, a crash) is indistinguishable
    # from a failing implementation, and has to be reconstructed by hand.
    agent_output: str = ""


@dataclass
class SessionLog:
    ticket: str
    started_at: str
    base_commit: str
    branch: str
    sandbox: str
    outcome: str = "incomplete"
    abort_reason: str | None = None
    iterations: list[Iteration] = field(default_factory=list)
    evidence: str | None = None


def commit_iteration(wt: Path, ticket_id: str, n: int) -> str:
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    if git(wt, "status", "--porcelain"):
        subprocess.run(
            ["git", "commit", "-q", "-m", f"{ticket_id}: agent iteration {n}"],
            cwd=wt, check=True,
        )
    return git(wt, "rev-parse", "HEAD")


# The outcomes that promoted evidence. Both are successes; they differ in what
# the evidence claims, which is recorded in the record rather than inferred here.
PROMOTED_OUTCOMES = frozenset({"passed", "passed_modulo_baseline"})


def full_gate_failure(ticket: dict, full: Record) -> Abort:
    """Which kind of stop a failed full_gate is. Returns the exception; the
    caller raises it, following check_kills.

    Only callable meaningfully once acceptance is green, which is the whole
    reason it can decide anything. At approve time a full_gate failure naming
    the frozen test is indistinguishable from the expected red, so approve only
    warns. Here the implementation exists and passes its own tests, so a failure
    landing inside a file the agent may not edit is not unfinished work - it is
    a gate no permitted edit clears.

    Runs no commands: full.commands already carries named_paths, matched against
    untruncated output when the verifier ran them.
    """
    blocked = [b for b in frozen_blocks(full.commands, ticket) if b.unwinnable]
    if blocked:
        return Unwinnable(
            "full_gate is unwinnable as written: "
            + "; ".join(b.describe() for b in blocked)
            + ". Acceptance passed, so this is not unfinished work - the failure "
            "is inside a frozen file the agent may not edit, and no rerun can "
            "clear it. Fix the ticket or the tool configuration, then re-approve."
        )

    # There is deliberately no branch here for "everything that failed was
    # already failing". That case does not reach this function any more: it
    # promotes, on Record.passed_modulo_baseline. It used to return Unwinnable
    # telling the operator to fix the pre-existing failures or re-approve with
    # --rebaseline, and the second half of that advice could not work - the
    # failures are inside the baseline by definition, so widening it leaves them
    # pre-existing and returns here again. More importantly the first half asks
    # a brownfield repo to go green before any ticket may promote, which is the
    # condition the baseline was built to survive rather than to report.
    if full.uncomparable_failures:
        # Say so rather than implying the ratchet was applied and cleared.
        return Abort(
            "acceptance passed but full_gate failed, and the baseline could not be "
            "applied to: " + ", ".join(full.uncomparable_failures)
            + " (no baseline entry, or a failure neither run could identify). "
            "Violations: " + "; ".join(full.violations)
        )

    introduced = {c: k for c, k in full.new_failures.items() if k}
    if introduced:
        return Abort(
            "acceptance passed but full_gate found failures this ticket introduced: "
            + "; ".join(f"{c} -> {', '.join(k)}" for c, k in introduced.items())
        )
    return Abort("acceptance passed but full_gate failed: " + "; ".join(full.violations))


def promote_evidence(root: Path, wt: Path, ticket_id: str, rec: Record) -> Path:
    """One record per ticket, committed beside the code it verifies."""
    dest = wt / ".edad" / "evidence" / f"{ticket_id}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(rec)
    # Both verdicts, always. A reader must be able to tell a green gate from one
    # that was merely no worse than its baseline without re-deriving either.
    payload["passed"] = rec.passed
    payload["passed_modulo_baseline"] = rec.passed_modulo_baseline
    dest.write_text(json.dumps(payload, indent=2) + "\n")
    subprocess.run(["git", "add", "-f", str(dest)], cwd=wt, check=True)
    how = "full gate passed" if rec.passed else "full gate green modulo baseline"
    subprocess.run(
        ["git", "commit", "-q", "-m", f"{ticket_id}: evidence ({how})"],
        cwd=wt, check=True,
    )
    return dest


def cmd_run(args) -> int:  # noqa: PLR0915  # linear driver; splitting hides the flow
    root = repo_root()
    ticket = load_ticket(root, args.ticket)
    preflight(root, ticket, args.sandbox, args.dry_run, args.network)

    base = git(root, "rev-parse", "HEAD")
    wt, branch = make_worktree(root, ticket["id"], base)
    log = SessionLog(
        ticket=ticket["id"],
        started_at=datetime.now(timezone.utc).isoformat(),
        base_commit=base,
        branch=branch,
        sandbox=args.sandbox,
    )

    prompt = initial_prompt(ticket, args.sandbox, args.network)
    if args.dry_run:
        print(prompt)
        print(f"\n[dry-run] worktree {wt} on {branch}; no agent invoked")
        return 0

    max_iter = (ticket.get("kill_conditions") or {}).get("max_iterations", 6)
    signatures: list[str] = []
    rec: Record | None = None
    prev_commit = base
    no_progress = 0

    try:
        for n in range(1, max_iter + 1):
            print(f"\n--- iteration {n}/{max_iter} ---")
            argv = agent_argv(prompt, wt, args.sandbox, args.image, args.yolo, args.network)
            t0 = time.monotonic()
            exit_code, out = run_agent(argv, wt)
            print(f"agent exited {exit_code} in {round(time.monotonic() - t0)}s")
            if exit_code == 124:
                raise Abort(out)

            commit = commit_iteration(wt, ticket["id"], n)
            made_commit = commit != prev_commit
            prev_commit = commit
            rec = evaluate(wt, ticket, "acceptance", base)
            write_record(root, rec)
            sig = failure_signature(rec)
            signatures.append(sig)
            log.iterations.append(
                Iteration(n, exit_code, commit, rec.passed, sig, list(rec.violations),
                          made_commit, out[-AGENT_TAIL:])
            )
            print(f"gate: {'PASS' if rec.passed else 'FAIL'}  {'; '.join(rec.violations)}")

            # Checked before check_kills: when the agent never ran, the gate's
            # failure signature describes the frozen test rather than the cause,
            # and aborting on it points at the one file that is not at fault.
            if exit_code != 0 and not made_commit:
                no_progress += 1
                if no_progress >= MAX_NO_PROGRESS:
                    raise Abort(
                        f"agent exited {exit_code} and committed nothing, "
                        f"{no_progress} iterations running - it is not failing the "
                        f"ticket, it is not running. Its last output:\n"
                        f"{out[-1000:]}"
                    )
            else:
                no_progress = 0

            reason = check_kills(ticket, rec, wt, base, signatures)
            if reason:
                raise Abort(reason)
            if rec.passed:
                break
            prompt = retry_prompt(ticket, rec, n, args.sandbox, args.network)
        else:
            raise Abort(f"exhausted {max_iter} iterations without passing the gate")

        full = evaluate(wt, ticket, "full_gate", base)
        write_record(root, full)
        if not (full.passed or full.passed_modulo_baseline):
            raise full_gate_failure(ticket, full)

        dest = promote_evidence(root, wt, ticket["id"], full)
        log.outcome = "passed" if full.passed else "passed_modulo_baseline"
        log.evidence = str(dest.relative_to(wt))
        how = "PASSED" if full.passed else (
            "PASSED modulo the approved baseline (the suite is not green; "
            "nothing failing is new)"
        )
        print(f"\n{how}. branch {branch}, evidence committed. Not merged — review and merge.")

    except Abort as e:
        log.outcome = "unwinnable" if isinstance(e, Unwinnable) else "aborted"
        log.abort_reason = str(e)
        print(f"\nABORTED: {e}\nBranch {branch} left at {wt} for inspection.", file=sys.stderr)
    finally:
        d = root / ".edad" / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        stamp = log.started_at.replace(":", "").replace("-", "")[:15]
        (d / f"{log.ticket}-{stamp}.json").write_text(json.dumps(asdict(log), indent=2) + "\n")

    return 0 if log.outcome in PROMOTED_OUTCOMES else 1


def build_parser() -> argparse.ArgumentParser:
    """The CLI, built apart from main() so the wiring is testable without
    spawning a process - a flag that reaches no code is a flag that silently
    does nothing."""
    p = argparse.ArgumentParser(prog="edad.session")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one ticket unattended")
    r.add_argument("ticket")
    r.add_argument("--sandbox", choices=["none", "docker"], default="none")
    r.add_argument("--image", default=DEFAULT_IMAGE)
    r.add_argument("--network", metavar="NAME", default=None,
                   help="attach the agent container to this docker network instead of "
                        "--network none; refused unless docker reports it internal")
    r.add_argument("--yolo", action="store_true",
                   help="skip permission prompts; only meaningful with --sandbox docker")
    r.add_argument("--dry-run", action="store_true", help="print the prompt and exit")
    r.set_defaults(func=cmd_run)
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except Abort as e:
        print(f"edad: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""
EDAD session controller.

Runs one ticket unattended. The loop terminates on the gate's exit code, never
on the agent's claim to be finished — an agent cannot produce a passing verdict
except by satisfying acceptance tests it is not permitted to edit.

  worktree  ->  [ prompt -> agent -> commit -> gate ]xN  ->  full gate  ->  evidence

Nothing is merged to a mainline branch. A finished session leaves a branch and
a signed-off record; the merge decision stays with a human.

Usage
    python -m edad.session run T001                    # local worktree
    python -m edad.session run T001 --sandbox docker   # network-isolated
    python -m edad.session run T001 --dry-run          # prompt only, no agent
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


def gate_toolchain_problems(root: Path) -> list[str]:
    """Compare the versions the GATE will resolve against requirements-gate.txt.

    Deliberately shells out rather than importing here: evaluate() runs its
    commands through a shell, so a pin satisfied inside this interpreter proves
    nothing about the one `python3 -m pytest` actually reaches. A worktree is a
    fresh checkout with no .venv, so an inherited PATH is the only thing making
    the pinned toolchain available - and if it is missing, the gate reports the
    toolchain's failure as the code's.
    """
    req = root / "requirements-gate.txt"
    if not req.exists():
        return []
    problems = []
    for raw in req.read_text().splitlines():
        line = raw.split("#")[0].strip()
        if "==" not in line:
            continue
        name, _, pinned = line.partition("==")
        name, pinned = name.strip(), pinned.strip()
        code = f"from importlib.metadata import version; print(version({name!r}))"
        proc = subprocess.run(
            f"python3 -c {shlex.quote(code)}",
            cwd=root, shell=True, capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            problems.append(f"{name}: not installed for the gate's python3 (pinned {pinned})")
        elif proc.stdout.strip() != pinned:
            problems.append(f"{name}: {proc.stdout.strip()}, pinned {pinned}")
    return problems


def preflight(root: Path, ticket: dict, sandbox: str, dry_run: bool) -> None:
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
    if ticket.get("status") != "approved":
        raise Abort(f"ticket status is {ticket.get('status')!r}, expected 'approved'")
    if not (root / ".edad" / "hashes" / f"{ticket['id']}.json").exists():
        raise Abort("no approval lock; run 'python -m edad.gate approve <ticket>'")

    ok, problems = check_freeze(root, ticket)
    if not ok:
        raise Abort("frozen files already differ from the approval lock: " + "; ".join(problems))

    blocked = ticket.get("blocked_by") or []
    unmet = [b for b in blocked if not (root / ".edad" / "evidence" / f"{b}.json").exists()]
    if unmet:
        raise Abort(f"blocked by tickets with no evidence: {', '.join(unmet)}")

    # changed_files() filters .edad/ — the harness's own logs and worktrees
    # must not count as user changes, or a session can never run twice.
    dirty = changed_files(root, None)
    if dirty:
        raise Abort("working tree is dirty; commit or stash first: " + ", ".join(dirty[:5]))
    if not dry_run:
        if not shutil.which("claude"):
            raise Abort("the 'claude' CLI is not on PATH")
        if not agent_has_credential():
            raise Abort(
                "the 'claude' CLI has no credential. Run 'claude setup-token' and "
                "export CLAUDE_CODE_OAUTH_TOKEN, or 'claude auth login'."
            )
        if sandbox == "docker" and not shutil.which("docker"):
            raise Abort("--sandbox docker requested but docker is not on PATH")


def agent_has_credential() -> bool:
    """Whether the CLI has *a* credential: an exported token or a keychain login.

    Requiring CLAUDE_CODE_OAUTH_TOKEN was wrong - a keychain login runs the
    agent fine, so that check refused sessions that would have worked. This is
    deliberately not a proof that the credential is *valid*: `claude auth
    status` reports loggedIn:true for a malformed token too, so a 401 still
    reaches the loop. MAX_NO_PROGRESS is what catches that.
    """
    proc = subprocess.run(
        ["claude", "auth", "status"], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        return False
    try:
        return bool(json.loads(proc.stdout).get("loggedIn"))
    except (json.JSONDecodeError, AttributeError):
        return False

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


def initial_prompt(ticket: dict, sandbox: str = "none") -> str:
    scope = "\n".join(f"  - {s}" for s in ticket.get("scope") or [])
    frozen = "\n".join(f"  - {s}" for s in ticket.get("frozen") or [])
    accept = "\n".join(f"  {c}" for c in ticket.get("acceptance") or [])
    # Rule 4 must describe the run it is in. Under --sandbox none the agent
    # does have the network, and asserting otherwise puts an unenforceable
    # claim in a prompt whose other rules are all mechanically checked.
    network = (
        "4. You have no network access: the container runs with --network none.\n"
        "   Do not attempt installs or downloads."
        if sandbox == "docker"
        else "4. Do not use the network: no installs, no downloads. The pinned\n"
        "   toolchain is already present and complete. This run is unsandboxed,\n"
        "   so unlike the rules above this one is not mechanically enforced. It\n"
        "   is still a requirement."
    )
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

{network}

Work directly in the repository. Do not create a summary, a report, or a
completion file; nothing you write about your work is read.
"""


def retry_prompt(ticket: dict, rec: Record, iteration: int) -> str:
    fails = "\n\n".join(
        f"$ {c.command}\nexit {c.exit_code}\n{c.output_tail[-1500:]}"
        for c in rec.commands
        if not c.ok
    )
    return f"""Iteration {iteration} of ticket {ticket['id']} did not pass the gate.

This is the verifier's own output, not a summary:

{fails}

Fix the implementation. The same constraints apply: only the files in scope,
the frozen tests stay untouched, no network.
"""


# --- agent invocation ------------------------------------------------------


def agent_argv(prompt: str, workdir: Path, sandbox: str, image: str, yolo: bool) -> list[str]:
    """Build the agent command.

    Flags differ across Claude Code releases — verify with `claude --help` and
    override with EDAD_AGENT_CMD if yours differs. The invocation is pinned
    explicitly rather than relying on defaults, because `-p` defaults are
    documented as changing in future releases.
    """
    override = os.environ.get("EDAD_AGENT_CMD")
    base = shlex.split(override) if override else ["claude", "-p"]
    perm = ["--dangerously-skip-permissions"] if yolo else ["--permission-mode", "acceptEdits"]
    inner = [*base, *perm, prompt]

    if sandbox != "docker":
        return inner
    return [
        "docker", "run", "--rm",
        "--network", "none",                     # the deny in kill_conditions, made real
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
    if k.get("diff_touches_outside_scope", True) and not rec.scope_ok:
        return "diff touched files outside the ticket's scope"
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


def promote_evidence(root: Path, wt: Path, ticket_id: str, rec: Record) -> Path:
    """One record per ticket, committed beside the code it verifies."""
    dest = wt / ".edad" / "evidence" / f"{ticket_id}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(rec)
    payload["passed"] = rec.passed
    dest.write_text(json.dumps(payload, indent=2) + "\n")
    subprocess.run(["git", "add", "-f", str(dest)], cwd=wt, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"{ticket_id}: evidence (full gate passed)"],
        cwd=wt, check=True,
    )
    return dest


def cmd_run(args) -> int:  # noqa: PLR0915  # linear driver; splitting hides the flow
    root = repo_root()
    ticket = load_ticket(root, args.ticket)
    preflight(root, ticket, args.sandbox, args.dry_run)

    base = git(root, "rev-parse", "HEAD")
    wt, branch = make_worktree(root, ticket["id"], base)
    log = SessionLog(
        ticket=ticket["id"],
        started_at=datetime.now(timezone.utc).isoformat(),
        base_commit=base,
        branch=branch,
        sandbox=args.sandbox,
    )

    prompt = initial_prompt(ticket, args.sandbox)
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
            argv = agent_argv(prompt, wt, args.sandbox, args.image, args.yolo)
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
            prompt = retry_prompt(ticket, rec, n)
        else:
            raise Abort(f"exhausted {max_iter} iterations without passing the gate")

        full = evaluate(wt, ticket, "full_gate", base)
        write_record(root, full)
        if not full.passed:
            raise Abort("acceptance passed but full_gate failed: " + "; ".join(full.violations))

        dest = promote_evidence(root, wt, ticket["id"], full)
        log.outcome = "passed"
        log.evidence = str(dest.relative_to(wt))
        print(f"\nPASSED. branch {branch}, evidence committed. Not merged — review and merge.")

    except Abort as e:
        log.outcome = "aborted"
        log.abort_reason = str(e)
        print(f"\nABORTED: {e}\nBranch {branch} left at {wt} for inspection.", file=sys.stderr)
    finally:
        d = root / ".edad" / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        stamp = log.started_at.replace(":", "").replace("-", "")[:15]
        (d / f"{log.ticket}-{stamp}.json").write_text(json.dumps(asdict(log), indent=2) + "\n")

    return 0 if log.outcome == "passed" else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="edad.session")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one ticket unattended")
    r.add_argument("ticket")
    r.add_argument("--sandbox", choices=["none", "docker"], default="none")
    r.add_argument("--image", default=DEFAULT_IMAGE)
    r.add_argument("--yolo", action="store_true",
                   help="skip permission prompts; only meaningful with --sandbox docker")
    r.add_argument("--dry-run", action="store_true", help="print the prompt and exit")
    r.set_defaults(func=cmd_run)
    args = p.parse_args()
    try:
        return args.func(args)
    except Abort as e:
        print(f"edad: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

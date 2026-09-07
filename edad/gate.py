"""
EDAD gate runner.

An independent verifier. It does not ask the agent whether it succeeded; it
re-derives the answer from the repository. Three checks, in order, all of which
must pass:

  1. FREEZE  - acceptance tests are byte-identical to what was approved
  2. SCOPE   - the diff touches only files the ticket declared
  3. GATE    - the ticket's acceptance commands exit 0

Output is a JSON record: ticket id, commit sha, per-check verdict, per-command
exit code and duration, and the tail of each command's output. That record is
the evidence. The agent's own account of its work is not an input here.

Usage
    python -m edad.gate approve T001
    python -m edad.gate run     T001 [--base-ref REF] [--full]
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

TAIL_CHARS = 4000


def repo_root() -> Path:
    env = os.environ.get("EDAD_REPO")
    if env:
        return Path(env).resolve()
    return Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )


def git_raw(root: Path, *args: str) -> str:
    """Unstripped stdout. Required for --porcelain, whose first two columns are
    status flags: stripping the output eats the leading space of line one and
    shifts every path by a character."""
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def git(root: Path, *args: str) -> str:
    return git_raw(root, *args).strip()


def load_ticket(root: Path, ticket_id: str) -> dict:
    path = root / ".edad" / "tickets" / f"{ticket_id}.md"
    if not path.exists():
        die(f"no ticket at {path}")
    text = path.read_text()
    if not text.startswith("---"):
        die(f"{path} has no YAML frontmatter")
    _, front, body = text.split("---", 2)
    meta = yaml.safe_load(front) or {}
    meta["_body"] = body.strip()
    meta["_path"] = str(path)
    return meta


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def die(msg: str) -> None:
    print(f"edad: {msg}", file=sys.stderr)
    raise SystemExit(2)


@dataclass
class CommandResult:
    command: str
    exit_code: int
    duration_s: float
    output_tail: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class Record:
    ticket: str
    started_at: str
    commit: str
    base_ref: str | None
    gate: str
    freeze_ok: bool
    scope_ok: bool
    commands_ok: bool
    violations: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    commands: list[CommandResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.freeze_ok and self.scope_ok and self.commands_ok


# --- checks ----------------------------------------------------------------


def check_freeze(root: Path, ticket: dict) -> tuple[bool, list[str]]:
    """Acceptance tests must be byte-identical to the approved snapshot."""
    frozen = ticket.get("frozen") or []
    if not frozen:
        return True, []
    lock_path = root / ".edad" / "hashes" / f"{ticket['id']}.json"
    if not lock_path.exists():
        return False, [f"no approval lock at {lock_path}; run 'approve' first"]
    approved = json.loads(lock_path.read_text())
    problems = []
    for rel in frozen:
        p = root / rel
        if not p.exists():
            problems.append(f"frozen file deleted: {rel}")
            continue
        actual = sha256(p)
        expected = approved.get(rel)
        if expected is None:
            problems.append(f"frozen file not in approval lock: {rel}")
        elif actual != expected:
            problems.append(
                f"frozen file modified: {rel} (approved {expected[:12]}, now {actual[:12]})"
            )
    return not problems, problems


# The harness writes here. These are not agent output and are never in scope.
INFRA_PREFIXES = (".edad/",)


def changed_files(root: Path, base_ref: str | None) -> list[str]:
    """Files the agent touched: committed since base_ref, plus anything dirty.

    Excludes .edad/ — those are the harness's own records and locks. Ignored
    files (__pycache__ etc.) are already absent from git status by default.
    """
    files: set[str] = set()
    if base_ref:
        out = git(root, "diff", "--name-only", f"{base_ref}...HEAD")
        files.update(f for f in out.splitlines() if f)
    # -uall expands untracked directories to individual files; without it a
    # brand-new package shows up as "ytmp3/" and never matches a scope glob.
    out = git_raw(root, "status", "--porcelain", "-uall")
    for line in out.splitlines():
        if len(line) > 3:
            files.add(line[3:].strip().split(" -> ")[-1])
    return sorted(f for f in files if not f.startswith(INFRA_PREFIXES))


def check_scope(ticket: dict, files: list[str]) -> tuple[bool, list[str]]:
    """Every changed file must match a declared scope glob."""
    allowed = list(ticket.get("scope") or [])
    if not allowed:
        return True, []
    # Frozen files are checked by hash; if unchanged they never appear here.
    allowed += list(ticket.get("frozen") or [])
    strays = [
        f
        for f in files
        if not any(fnmatch.fnmatch(f, pat) for pat in allowed)
    ]
    return not strays, [f"out of scope: {f}" for f in strays]


def run_commands(root: Path, commands: list[str], deny_network: bool) -> list[CommandResult]:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # never let a gate run bill an API account
    if deny_network:
        env["EDAD_NETWORK"] = "deny"
    results = []
    for cmd in commands:
        t0 = time.monotonic()
        # check=False is deliberate: a non-zero exit is the measurement,
        # not an error. Explicit so the intent survives a linter upgrade.
        proc = subprocess.run(
            cmd, cwd=root, shell=True, capture_output=True, text=True, env=env,
            check=False,
        )
        out = (proc.stdout + proc.stderr)[-TAIL_CHARS:]
        results.append(
            CommandResult(cmd, proc.returncode, round(time.monotonic() - t0, 2), out)
        )
    return results


def gate_toolchain_problems(root: Path) -> list[str]:
    """Compare the versions the GATE will resolve against requirements-gate.txt.

    Deliberately shells out rather than importing here: run_commands executes
    through a shell, so a pin satisfied inside this interpreter proves nothing
    about the one `python3 -m pytest` actually reaches. Both approve and the
    session controller need this - approve because a missing pytest makes the
    acceptance commands "fail" for the wrong reason, which reads as red and
    would let a vacuous test through the check below.
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


# --- commands --------------------------------------------------------------


def cmd_approve(args) -> int:
    root = repo_root()
    ticket = load_ticket(root, args.ticket)
    frozen = ticket.get("frozen") or []
    if not frozen:
        die("ticket declares no frozen files; nothing to approve")
    for rel in frozen:
        if not (root / rel).exists():
            die(f"frozen file does not exist: {rel}")

    # Everything downstream rests on the frozen tests having teeth. Nothing
    # checked that. A test that asserts nothing passes before the work exists,
    # sails through the gate on the first iteration, and promotes evidence for
    # an implementation nobody wrote - the freeze mechanism faithfully
    # protecting a contract that says nothing.
    red = []
    commands = ticket.get("acceptance") or []
    if commands and not args.allow_passing:
        problems = gate_toolchain_problems(root)
        if problems:
            die(
                "cannot prove the acceptance commands fail: the gate's toolchain does "
                "not match requirements-gate.txt: " + "; ".join(problems) + ". A missing "
                "tool fails for the wrong reason and would read as red. Install the "
                "pins: pip install -r requirements-gate.txt"
            )
        kills = ticket.get("kill_conditions") or {}
        red = run_commands(
            root, commands, deny_network=(kills.get("network_access") == "deny")
        )
        if all(c.ok for c in red):
            die(
                "acceptance commands already pass, so freezing them proves nothing: "
                "either they assert nothing, or the implementation already exists. "
                "Approval happens before the work. Re-run with --allow-passing only "
                "if you are deliberately re-approving a ticket already implemented."
            )

    hashes = {rel: sha256(root / rel) for rel in frozen}
    lock = dict(hashes)
    # Reserved key: frozen entries are relative paths and never collide with it.
    lock["_edad"] = {
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "red_proof": [
            {"command": c.command, "exit_code": c.exit_code} for c in red if not c.ok
        ]
        or None,
    }
    out = root / ".edad" / "hashes" / f"{ticket['id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(lock, indent=2) + "\n")
    print(f"approved {ticket['id']}: {len(hashes)} frozen file(s)")
    for rel, h in hashes.items():
        print(f"  {h[:12]}  {rel}")
    if red:
        print("  red proof (these failed before the work existed):")
        for c in red:
            print(f"    [{c.exit_code}] {c.command}")
    else:
        print("  ! no red proof recorded (--allow-passing)")
    return 0


def evaluate(
    root: Path, ticket: dict, gate_name: str = "acceptance", base_ref: str | None = None
) -> Record:
    """Run the three checks and return a Record. The single entry point for
    anything that needs a verdict — the CLI and the session controller both
    call this, so they cannot drift apart."""
    kills = ticket.get("kill_conditions") or {}
    commands = ticket.get(gate_name) or []
    if not commands:
        die(f"ticket declares no '{gate_name}' commands")

    rec = Record(
        ticket=ticket["id"],
        started_at=datetime.now(timezone.utc).isoformat(),
        commit=git(root, "rev-parse", "HEAD"),
        base_ref=base_ref,
        gate=gate_name,
        freeze_ok=True,
        scope_ok=True,
        commands_ok=False,
    )

    rec.freeze_ok, freeze_problems = check_freeze(root, ticket)
    rec.violations += freeze_problems

    rec.changed_files = changed_files(root, base_ref)
    rec.scope_ok, scope_problems = check_scope(ticket, rec.changed_files)
    if kills.get("diff_touches_outside_scope", True):
        rec.violations += scope_problems
    else:
        rec.scope_ok = True

    # A tampered acceptance test invalidates the run. Do not execute it.
    if not rec.freeze_ok:
        rec.commands_ok = False
    else:
        rec.commands = run_commands(
            root, commands, deny_network=(kills.get("network_access") == "deny")
        )
        rec.commands_ok = all(c.ok for c in rec.commands)
        rec.violations += [
            f"command failed ({c.exit_code}): {c.command}" for c in rec.commands if not c.ok
        ]

    return rec


def cmd_run(args) -> int:
    root = repo_root()
    ticket = load_ticket(root, args.ticket)
    gate_name = "full_gate" if args.full else "acceptance"
    rec = evaluate(root, ticket, gate_name, args.base_ref)
    write_record(root, rec)
    report(rec)
    return 0 if rec.passed else 1


def write_record(root: Path, rec: Record) -> Path:
    d = root / ".edad" / "records"
    d.mkdir(parents=True, exist_ok=True)
    stamp = rec.started_at.replace(":", "").replace("-", "")[:15]
    path = d / f"{rec.ticket}-{stamp}-{rec.commit[:8]}.json"
    payload = asdict(rec)
    payload["passed"] = rec.passed
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def report(rec: Record) -> None:
    def mark(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    print(f"\n{rec.ticket} @ {rec.commit[:8]}  [{rec.gate}]")
    print(f"  freeze   {mark(rec.freeze_ok)}")
    print(f"  scope    {mark(rec.scope_ok)}  ({len(rec.changed_files)} file(s) changed)")
    print(f"  commands {mark(rec.commands_ok)}")
    for c in rec.commands:
        print(f"    [{c.exit_code}] {c.duration_s}s  {c.command}")
    for v in rec.violations:
        print(f"  ! {v}")
    print(f"  => {mark(rec.passed)}\n")


def main() -> int:
    p = argparse.ArgumentParser(prog="edad.gate")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("approve", help="record hashes of the ticket's frozen files")
    a.add_argument("ticket")
    a.add_argument("--allow-passing", action="store_true",
                   help="approve even if the acceptance commands already pass")
    a.set_defaults(func=cmd_approve)

    r = sub.add_parser("run", help="verify a ticket and write an evidence record")
    r.add_argument("ticket")
    r.add_argument("--base-ref", default=None, help="compare diff against this ref")
    r.add_argument("--full", action="store_true", help="run full_gate instead of acceptance")
    r.set_defaults(func=cmd_run)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

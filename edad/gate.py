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
import re
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


def ticket_path(root: Path, ticket_id: str) -> Path:
    """Where the ticket lives in a given tree. A function, not ticket["_path"],
    because the two differ where it matters: the controller loads the ticket
    from the main repo and then evaluates against a worktree. check_freeze must
    hash the copy in the tree it is verifying - .edad/ is in INFRA_PREFIXES and
    so is invisible to the scope check, which means an agent editing the ticket
    inside its own worktree is exactly the case that needs catching."""
    return root / ".edad" / "tickets" / f"{ticket_id}.md"


def load_ticket(root: Path, ticket_id: str) -> dict:
    path = ticket_path(root, ticket_id)
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
    # Traceability, carried from the ticket and the approval lock so the record
    # stands alone. Without these a passing record says "this command exited 0"
    # and stops there: which design decision it discharges is in a grill
    # transcript, and the proof that the same command could fail is in a lock
    # file nothing cites. Together they let the record say what an auditor
    # actually asks - this test was proven capable of failing, for decision D1,
    # before the work began, and it passes now.
    decisions: list[str] = field(default_factory=list)
    red_proof: list[dict] | None = None
    # Whether an approval lock with an _edad block was found at all. Without it
    # a missing red_proof cannot be read as "approved with --allow-passing" -
    # see report(), which used to assert that flag either way.
    approved: bool = False
    # Whether that lock pinned the ticket's own bytes. A lock predating that
    # field still verifies the frozen tests, but cannot say the ticket was the
    # one approved - a weaker claim, and the record should say which it makes.
    ticket_verified: bool = False

    @property
    def passed(self) -> bool:
        return self.freeze_ok and self.scope_ok and self.commands_ok


# --- checks ----------------------------------------------------------------


def check_freeze(root: Path, ticket: dict) -> tuple[bool, list[str]]:
    """The acceptance tests AND the ticket itself must be byte-identical to what
    was approved.

    Hashing only the frozen tests protected the contract's tests while leaving
    the contract editable. The ticket sits under .edad/, which INFRA_PREFIXES
    excludes from every scope check, so it was neither hashed nor diffed:
    acceptance, scope, kill_conditions and decisions could all be rewritten
    after approval with nothing in the system able to notice. Decisions were
    just the first field where that showed. One hash over the file closes the
    whole class.
    """
    frozen = ticket.get("frozen") or []
    lock_path = root / ".edad" / "hashes" / f"{ticket['id']}.json"
    if not lock_path.exists():
        if not frozen:
            return True, []
        return False, [f"no approval lock at {lock_path}; run 'approve' first"]
    approved = json.loads(lock_path.read_text())
    problems = []

    meta = approved.get(LOCK_META_KEY)
    expected_ticket = meta.get("ticket_sha256") if isinstance(meta, dict) else None
    if expected_ticket:
        tp = ticket_path(root, ticket["id"])
        if not tp.exists():
            problems.append(f"ticket file deleted: {tp}")
        else:
            actual = sha256(tp)
            if actual != expected_ticket:
                problems.append(
                    f"ticket modified since approval: {tp.name} "
                    f"(approved {expected_ticket[:12]}, now {actual[:12]}). "
                    f"Every field it declares - acceptance, scope, "
                    f"kill_conditions, decisions - is part of the contract. "
                    f"Re-approve to adopt the change."
                )

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


LOCK_META_KEY = "_edad"


def approval_meta(root: Path, ticket_id: str) -> dict:
    """The `_edad` block of the approval lock, or {} when there is no lock.

    Separate from check_freeze, which only needs the hashes. This is what makes
    the red proof citable: approve records it, and evaluate copies it into the
    record instead of leaving it in a file nothing reads.
    """
    lock_path = root / ".edad" / "hashes" / f"{ticket_id}.json"
    if not lock_path.exists():
        return {}
    try:
        lock = json.loads(lock_path.read_text())
    except json.JSONDecodeError:
        return {}
    meta = lock.get(LOCK_META_KEY)
    return meta if isinstance(meta, dict) else {}


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


# How to ask a tool its version when importlib.metadata cannot see it.
# importlib.metadata only knows about installed *Python distributions*, so a
# Homebrew or standalone-installer ruff — the two common install paths on macOS
# — is on PATH, is the binary the gate will actually run, and is invisible to
# it. Reporting that as "not installed" points at the wrong cause and refuses
# every approve and every session. A pure library with no CLI has no entry
# here: metadata is the only way to see it, and its absence really is missing.
VERSION_PROBES = {
    "pytest": "python3 -m pytest --version",
    "ruff": "ruff --version",
}
_VERSION_RE = re.compile(r"\b(\d+(?:\.\d+)+)\b")


def _probe_versions(root: Path, name: str) -> tuple[str | None, str | None]:
    """(version importlib.metadata sees, version the PATH binary reports).

    Both, deliberately, because which one the gate actually runs is a property
    of how the command was written: `python3 -m pytest` resolves through
    metadata, a bare `ruff check .` runs whatever is first on PATH. Returning
    one and guessing hides exactly the drift this module exists to catch - a
    pip-installed ruff satisfying the pin while an older one earlier on PATH is
    the binary full_gate shells out to.
    """
    code = f"from importlib.metadata import version; print(version({name!r}))"
    proc = subprocess.run(
        f"python3 -c {shlex.quote(code)}",
        cwd=root, shell=True, capture_output=True, text=True, check=False,
    )
    meta = proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None

    on_path = None
    probe = VERSION_PROBES.get(name)
    if probe:
        proc = subprocess.run(
            probe, cwd=root, shell=True, capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            m = _VERSION_RE.search(proc.stdout + proc.stderr)
            on_path = m.group(1) if m else None
    return meta, on_path


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
        meta, on_path = _probe_versions(root, name)
        found = meta or on_path
        if found is None:
            where = "the gate's python3 or PATH" if name in VERSION_PROBES else "the gate's python3"
            problems.append(f"{name}: not found on {where} (pinned {pinned})")
        elif meta and on_path and meta != on_path:
            # Neither is wrong; they disagree, and which one runs depends on how
            # each command is spelled. That ambiguity is itself the defect: the
            # verdict must be a property of the commit, not of argv.
            problems.append(
                f"{name}: {meta} importable but {on_path} first on PATH "
                f"(pinned {pinned}); which one runs depends on how the command "
                f"is written"
            )
        elif found != pinned:
            problems.append(f"{name}: {found}, pinned {pinned}")
    return problems


def _command_prefix(cmd: str, n: int = 3) -> tuple[str, ...]:
    try:
        tokens = shlex.split(cmd)
    except ValueError:  # unbalanced quotes; the raw split is good enough here
        tokens = cmd.split()
    return tuple(tokens[:n])


def unwinnable_full_gate(root: Path, ticket: dict) -> list[str]:
    """Refuse a ticket whose full_gate no implementation could ever clear.

    full_gate runs repo-wide at session end, after acceptance has passed. A
    failure it reports *inside a frozen file* is unclearable by construction:
    the agent may not edit that file, so the session spends every iteration
    turning acceptance green and then dies on a lint error nobody is permitted
    to fix - promoting no evidence, ever. The ticket format already says
    full_gate must be winnable; this makes that a check rather than advice.

    Two deliberate blind spots, both erring toward silence:

    - A full_gate command sharing its first three tokens with an acceptance
      command is skipped. Before the implementation exists those are *supposed*
      to be red, and they fail naming the frozen test - so testing them would
      call every well-formed ticket unwinnable. This can therefore miss a
      genuinely unwinnable repo-wide run; it will not invent one.
    - "Names a frozen path" is a substring test against the command's output
      tail. A tool that prints paths in another shape, or a run long enough to
      push the mention past TAIL_CHARS, slips through.

    A check that blocks approval should fail silent, not loud. A false negative
    costs one wasted session; a false positive blocks work that was fine.
    """
    frozen = ticket.get("frozen") or []
    full = ticket.get("full_gate") or []
    if not frozen or not full:
        return []

    skip = {_command_prefix(c) for c in ticket.get("acceptance") or []}
    to_run = [c for c in full if _command_prefix(c) not in skip]
    if not to_run:
        return []

    kills = ticket.get("kill_conditions") or {}
    results = run_commands(
        root, to_run, deny_network=(kills.get("network_access") == "deny")
    )
    problems = []
    for c in results:
        if c.ok:
            continue
        named = [rel for rel in frozen if rel in c.output_tail]
        if named:
            problems.append(
                f"{c.command!r} exits {c.exit_code} on frozen file(s) "
                f"{', '.join(named)}"
            )
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

    commands = ticket.get("acceptance") or []
    prove_red = bool(commands) and not args.allow_passing

    # Both checks below execute commands, and both misread a broken toolchain:
    # a missing pytest fails for the wrong reason, which reads as red proof the
    # ticket has not earned and as a full_gate failure nobody can fix.
    if prove_red or ticket.get("full_gate"):
        problems = gate_toolchain_problems(root)
        if problems:
            die(
                "the gate's toolchain does not match requirements-gate.txt: "
                + "; ".join(problems) + ". A missing tool fails for the wrong reason "
                "and would read as red. Install the pins: "
                "pip install -r requirements-gate.txt"
            )

    unwinnable = unwinnable_full_gate(root, ticket)
    if unwinnable:
        die(
            "full_gate is unwinnable for this ticket: " + "; ".join(unwinnable) + ". "
            "The agent may not edit a frozen file, so no implementation clears this "
            "and the session would promote no evidence. Fix the repo or the tool's "
            "configuration - editing the frozen file invalidates the lock."
        )

    red = []
    # Everything downstream rests on the frozen tests having teeth. Nothing
    # checked that. A test that asserts nothing passes before the work exists,
    # sails through the gate on the first iteration, and promotes evidence for
    # an implementation nobody wrote - the freeze mechanism faithfully
    # protecting a contract that says nothing.
    if prove_red:
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
    lock[LOCK_META_KEY] = {
        "approved_at": datetime.now(timezone.utc).isoformat(),
        # The ticket file's own bytes, so the contract is tamper-evident and not
        # just the tests it points at. Everything else in this block is data
        # copied OUT of the ticket; this is what makes the ticket itself
        # citable, and what lets the fields below be trusted at read time.
        "ticket_sha256": sha256(ticket_path(root, ticket["id"])),
        # The decisions this ticket discharges, copied from the ticket so the
        # lock and every record derived from it name them without re-reading
        # the ticket, which may have been edited since.
        "decisions": list(ticket.get("decisions") or []),
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

    meta = approval_meta(root, ticket["id"])
    # The lock wins: it records the approval, the ticket only proposes it.
    # check_freeze now hashes the ticket too, so the two agree or the run has
    # already failed - which makes reading the approval record first a
    # redundancy rather than the thing holding the property up. Before that
    # hash existed this line was the property, and it had it backwards.
    rec.approved = bool(meta)
    rec.ticket_verified = bool(meta.get("ticket_sha256"))
    if "decisions" in meta:
        # Presence, not truthiness: an approval that recorded [] is asserting
        # this ticket discharges no decisions. `or` would treat that answer as
        # a missing one and fall through to the ticket - reintroducing the bug.
        rec.decisions = list(meta["decisions"])
    else:
        # No lock, or one predating the _edad block: nothing was captured at
        # approval, so the ticket is the only source available. It carries none
        # of the lock's guarantees.
        rec.decisions = list(ticket.get("decisions") or [])
    rec.red_proof = meta.get("red_proof")

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
    if rec.decisions:
        print(f"  decisions {', '.join(rec.decisions)}")
    if rec.approved and not rec.ticket_verified:
        print("  ! approval lock predates ticket hashing: the frozen tests were "
              "verified, the ticket's own fields were not")
    if rec.red_proof:
        print(f"  red proof at approval: {len(rec.red_proof)} command(s) failed")
    elif rec.commands_ok and rec.approved:
        # A pass with no red proof is a weaker claim, and saying so is the
        # difference between "the test passes" and "a test that could fail,
        # passes". Not a failure - --allow-passing is legitimate - but the
        # record should not let a reader assume the stronger claim.
        print("  ! no red proof at approval (--allow-passing): "
              "this pass does not show the test can fail")
    elif rec.commands_ok:
        # Distinct from the case above: there is no approval metadata to have
        # recorded a proof. Naming --allow-passing here would blame a flag
        # nobody passed.
        print("  ! no approval metadata for this ticket: this pass cites no red "
              "proof, and none was recorded. Run approve to establish one.")
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

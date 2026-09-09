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
    python3 -m edad.gate approve T001
    python3 -m edad.gate run     T001 [--base-ref REF] [--full]
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
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

TAIL_CHARS = 4000

# Every gate command is killed eventually. The default exists because the
# failure it prevents is unattended and silent: a legacy test that blocks on
# stdin, or a socket with no timeout of its own, hangs the gate forever. The
# session controller times out the AGENT but nothing timed out the VERIFIER, so
# that hang produced no verdict, no record, and no session log - the one
# failure mode that leaves nothing behind to diagnose. A ticket can raise or
# lower it with kill_conditions.command_timeout_s; it cannot switch it off.
DEFAULT_COMMAND_TIMEOUT_S = 900
TIMEOUT_EXIT_CODE = 124  # what timeout(1) and the shell convention use


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


def warn(msg: str) -> None:
    print(f"edad: warning: {msg}", file=sys.stderr)


@dataclass
class CommandResult:
    command: str
    exit_code: int
    duration_s: float
    output_tail: str
    # Which declared paths this command's output mentioned, matched against the
    # FULL output before it was truncated into output_tail. Scanning the tail
    # later is lossy in a way that fails silently: a long run pushes the mention
    # past TAIL_CHARS and the match just disappears. Computed once by the
    # verifier and stored, so it is evidence rather than something re-derived
    # from a lossy artifact - and it lands in the record JSON, which means the
    # record itself says which frozen files a failure implicated.
    named_paths: list[str] = field(default_factory=list)
    # A killed command did not RUN, and that is a different fact from a command
    # that ran and failed. Without this the two are one non-zero exit code, and
    # the session's retry prompt hands the agent a timeout as though it were a
    # test failure - sending it to fix a test that never reported a result.
    timed_out: bool = False
    # Computed by the verifier from the untruncated output, for the same reason
    # named_paths is: a failure past TAIL_CHARS vanishes from the tail, and a
    # baseline comparison re-derived from a lossy copy concludes "nothing new"
    # for failures it simply could not see.
    failure_keys: dict[str, int] | None = None

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
    commands_ok: bool
    # Scope is two separate facts, and collapsing them was a bug in the record
    # itself. `scope_violations` is what the verifier MEASURED: every changed
    # file that matched no declared pattern, recorded whether or not the ticket
    # asked for the check to be binding. `scope_enforced` is the POLICY, copied
    # from kill_conditions.diff_touches_outside_scope.
    #
    # Previously a ticket setting that kill condition false made evaluate()
    # assign scope_ok = True outright, so the record asserted a clean scope the
    # gate had just watched fail. That is the same class as a record naming
    # decisions it never verified: the artifact claiming something it did not
    # derive. Everything in this harness rests on the record being a report of
    # what happened, so the two facts stay separate and `passed` combines them.
    scope_violations: list[str] = field(default_factory=list)
    scope_enforced: bool = True
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
    # The other proof a ticket can carry, and a separate field rather than a
    # differently-shaped red_proof: no red was ever taken here, so filling that
    # one in would make the record assert something the gate never measured. The
    # two are never both set, and report() prints three tiers because there are
    # three.
    mutation_proof: dict | None = None
    # Whether an approval lock with an _edad block was found at all. Without it
    # a missing red_proof cannot be read as "approved with --allow-passing" -
    # see report(), which used to assert that flag either way.
    approved: bool = False
    # Whether that lock pinned the ticket's own bytes. A lock predating that
    # field still verifies the frozen tests, but cannot say the ticket was the
    # one approved - a weaker claim, and the record should say which it makes.
    ticket_verified: bool = False
    # The ratchet, on full_gate runs: what this run's failures were compared
    # against, and what survived the comparison. Stored rather than re-derived,
    # so the evidence record says which baseline it was judged against instead
    # of leaving a reader to assume the current one.
    baseline_commit: str | None = None
    new_failures: dict[str, list[str]] = field(default_factory=dict)
    # Failing commands the ratchet could not be applied to - no baseline entry,
    # or a failure neither side could identify. Named, not silently folded into
    # either answer: "we could not tell" is its own result, and treating it as
    # "nothing new" is exactly how a ratchet certifies a regression.
    uncomparable_failures: list[str] = field(default_factory=list)

    @property
    def pre_existing_only(self) -> bool:
        """Every failing command failed only in ways the baseline already had.

        This is what makes a pre-existing failure attributable to the repo
        rather than to the agent. False when nothing failed, false when any
        failure could not be compared, and false when any failure was never
        compared at all - each is a case where this answer would be an
        assumption rather than a measurement.
        """
        if self.commands_ok or self.uncomparable_failures:
            return False
        # The third unmeasured case, and the one this property used to answer
        # as though it had measured it. apply_ratchet runs on full_gate alone,
        # so on any other gate every failure goes unjudged - and the empty
        # new_failures that leaves behind is byte-identical to the one a clean
        # comparison leaves. An acceptance run with 25 red commands and no
        # baseline consulted therefore reported "nothing failing is new".
        # A failure the ratchet never looked at is a cannot-tell, exactly like
        # uncomparable_failures above; silence is not absolution.
        if any(c.command not in self.new_failures for c in self.commands if not c.ok):
            return False
        return not any(self.new_failures.values())

    @property
    def scope_ok(self) -> bool:
        """The measurement: did the diff stay inside the declared scope.

        Independent of whether the ticket made it binding. A reader asking
        "was this diff in scope" gets the answer the gate actually computed.
        """
        return not self.scope_violations

    @property
    def passed(self) -> bool:
        """The verdict, which is where policy applies - not in the measurement."""
        scope_clears = self.scope_ok or not self.scope_enforced
        return self.freeze_ok and scope_clears and self.commands_ok

    @property
    def passed_modulo_baseline(self) -> bool:
        """Green against the baseline rather than green outright.

        Deliberately a second, weaker property instead of widening `passed`:
        commands DID fail, and a record must never answer "was the suite green"
        with yes when it was not. This answers the different question the
        ratchet exists to ask - did this ticket make anything worse - and says
        so in its name.

        It is what makes the ratchet worth having. A brownfield repo carries red
        nobody is going to clear, so requiring `passed` there means no ticket
        ever promotes evidence and the baseline only improves an error message.
        Freeze and scope still have to be clean: those are this ticket's own
        conduct, and no baseline forgives them.
        """
        scope_clears = self.scope_ok or not self.scope_enforced
        return self.freeze_ok and scope_clears and self.pre_existing_only


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


def match_scope(pattern: str, path: str) -> bool:
    """Match one scope pattern against one repo-relative path, segment by segment.

    `fnmatch` on the whole string was wrong here, and wrong in the direction
    that matters: its `*` crosses `/`, so `ytmp3/*` admitted `ytmp3/a/b/c` and a
    bare `*.py` admitted every Python file in the repo - legacy/, edad/, the
    gate that is supposed to be judging the diff. `*` alone admitted
    `.github/workflows/ci.yml`, which is an agent editing the thing that would
    catch it.

    Nobody writing a ticket means that. `scope` is the containment mechanism, so
    its patterns match the way the person writing them expects: `*` stays inside
    one directory, and `**` is the explicit opt-in for crossing.

    fnmatchcase, not fnmatch: fnmatch normalises case through os.path.normcase,
    which is identity on Linux and macOS and lowercasing on Windows. Git paths
    are case-sensitive, and the verdict must be a property of the commit rather
    than of the machine that ran the gate.
    """
    return _match_parts(pattern.split("/"), path.split("/"))


def _match_parts(pattern: list[str], parts: list[str]) -> bool:
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        # Zero or more segments, so `**/*.py` covers both `a.py` and `x/y/a.py`.
        return any(_match_parts(rest, parts[i:]) for i in range(len(parts) + 1))
    if not parts or not fnmatch.fnmatchcase(parts[0], head):
        return False
    return _match_parts(rest, parts[1:])


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
        if not any(match_scope(pat, f) for pat in allowed)
    ]
    return not strays, [f"out of scope: {f}" for f in strays]


def command_timeout(ticket: dict) -> int:
    """Seconds any one gate command may run. Never None: an absent or unusable
    kill condition falls back to the default rather than to no limit, because
    the unbounded case is the one that hangs a session with nothing recorded."""
    raw = (ticket.get("kill_conditions") or {}).get("command_timeout_s")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return DEFAULT_COMMAND_TIMEOUT_S
    return raw


def run_commands(
    root: Path,
    commands: list[str],
    deny_network: bool,
    flag_paths: list[str] | tuple[str, ...] = (),
    timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S,
) -> list[CommandResult]:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # never let a gate run bill an API account
    if deny_network:
        env["EDAD_NETWORK"] = "deny"
    results = []
    for cmd in commands:
        t0 = time.monotonic()
        # check=False is deliberate: a non-zero exit is the measurement,
        # not an error. Explicit so the intent survives a linter upgrade.
        timed_out = False
        try:
            proc = subprocess.run(
                cmd, cwd=root, shell=True, capture_output=True, text=True, env=env,
                check=False, timeout=timeout_s,
            )
            exit_code, out = proc.returncode, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as e:
            # Keep whatever it managed to emit: with a hang, the last few lines
            # before the block are the diagnosis, and discarding them leaves the
            # record saying only that something took too long.
            timed_out = True
            exit_code = TIMEOUT_EXIT_CODE
            # TimeoutExpired carries the raw buffers: text=True governs the
            # decoding subprocess.run does on the normal path, not what the
            # exception holds, so these arrive as bytes and concatenating them
            # with a str raises inside the handler for a hang - swallowing the
            # partial output and the timeout together.
            partial = "".join(
                b.decode(errors="replace") if isinstance(b, bytes) else b
                for b in (e.stdout, e.stderr)
                if b
            )
            out = (
                f"{partial}\n"
                f"edad: killed after {timeout_s}s (kill_conditions.command_timeout_s). "
                f"This command did not finish, so it reported no result: the output "
                f"above is partial and there is no verdict on the code under test."
            )
        # Both derived from the full output, before the line below truncates it.
        named = [rel for rel in flag_paths if rel in out]
        if exit_code == 0:
            keys: dict[str, int] | None = {}  # ran clean: no failures, not unknown
        elif timed_out:
            keys = None  # reported no result at all; nothing to compare
        else:
            keys = extract_failure_keys(out)
        results.append(
            CommandResult(
                cmd, exit_code, round(time.monotonic() - t0, 2),
                out[-TAIL_CHARS:], named, timed_out, keys,
            )
        )
    return results


# Failure identity: a key stable enough to compare one run against another.
#
# The keys are deliberately coarse in different ways per tool, because the churn
# is per tool. A pytest node id is stable across edits and is used as-is. A lint
# finding is not: its line and column move every time anything above it changes,
# so keying on them turns one unfixed finding into a fresh failure on every
# commit and the ratchet never holds. File plus rule code is the coarsest key
# that still distinguishes findings, and that coarseness is the accepted cost.
PYTEST_FAILURE_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
# FAILED alone, and a SECOND pattern rather than a narrowing of the one above.
# The keys that one produces are persisted - every approval lock stores a
# full_gate_baseline of them, and existing locks hold ERROR-shaped keys - so
# folding the outcome into the key shape would make every stored baseline
# uncomparable and turn pre-existing failures into new ones repo-wide. The
# ratchet wants a coarse identity; the mutation proof wants the distinction the
# ratchet discards. They are different questions, so they get different regexes.
PYTEST_FAILED_RE = re.compile(r"^FAILED (\S+)", re.MULTILINE)
RUFF_FAILURE_RE = re.compile(r"^(\S+?):\d+:\d+: ([A-Z]+[0-9]+)\b", re.MULTILINE)
# ruff's DEFAULT output is the "full" diagnostic, which puts the rule on one
# line and the location on the next behind an arrow. The concise pattern above
# matches none of it, so before this existed every ruff failure came back
# unidentifiable and `ruff check .` could never be ratcheted - the baseline
# recorded a lint failure it could not name, and a session that tripped one
# aborted saying the baseline could not be applied rather than naming the rule.
# Both shapes are parsed because a ticket may declare --output-format=concise,
# and the gate runs whatever the ticket declares.
RUFF_FULL_FAILURE_RE = re.compile(
    r"^([A-Z]+[0-9]+)\b.*\n\s*-->\s+(\S+?):\d+:\d+", re.MULTILINE
)


def extract_failure_keys(output: str) -> dict[str, int] | None:
    """Identities of the failures in one command's FULL output, with counts.

    None means "nothing recognisable" - the command failed in a way this
    function cannot name. That is distinct from {} ("ran, no failures"), and the
    distinction is load-bearing: an unrecognised failure must never be compared
    against a baseline, because the comparison would silently conclude that
    nothing new is wrong. Callers treat None as "cannot ratchet this command".

    Counts, not a set, because the lint key is coarse. Three E501s in one file
    collapse to one key, so without counts a baseline holding one of them would
    absorb the other two - exactly the stale-baseline absorption the coarse key
    otherwise invites.
    """
    keys: dict[str, int] = {}
    for node in PYTEST_FAILURE_RE.findall(output):
        keys[f"pytest:{node}"] = keys.get(f"pytest:{node}", 0) + 1
    for path, rule in RUFF_FAILURE_RE.findall(output):
        k = f"lint:{path}:{rule}"
        keys[k] = keys.get(k, 0) + 1
    for rule, path in RUFF_FULL_FAILURE_RE.findall(output):
        k = f"lint:{path}:{rule}"
        keys[k] = keys.get(k, 0) + 1
    return keys or None


def new_failure_keys(
    now: dict[str, int] | None, baseline: dict[str, int] | None
) -> list[str] | None:
    """Keys failing more now than the baseline recorded. None when the two
    cannot be compared at all, which is never the same answer as "nothing new".
    """
    if now is None or baseline is None:
        return None
    return sorted(k for k, n in now.items() if n > baseline.get(k, 0))


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


def declared_paths(ticket: dict) -> list[str]:
    """The paths whose mention in command output is worth recording: the frozen
    files the agent may not touch, and the in-scope files it may. The split
    between them is what makes a failure diagnosable - see FrozenBlock."""
    return list(ticket.get("frozen") or []) + list(ticket.get("scope") or [])


@dataclass
class FrozenBlock:
    """A failing command whose output named at least one frozen file."""

    command: str
    exit_code: int
    frozen: list[str]
    fixable: list[str]

    @property
    def unwinnable(self) -> bool:
        """True only when nothing the agent was allowed to edit is implicated.

        A failure can name a frozen file *and* a file in scope - a test module
        that imports both, a linter reporting several files in one run. That is
        an ordinary failure that happens to mention a frozen path, and the fix
        is in the agent's hands. Calling it unwinnable would send someone off to
        rewrite a ticket that was fine.
        """
        return not self.fixable

    def describe(self) -> str:
        s = (f"{self.command!r} exits {self.exit_code} inside frozen "
             f"{', '.join(self.frozen)}")
        if self.fixable:
            s += f" (also names in-scope {', '.join(self.fixable)})"
        return s


def frozen_blocks(results: list[CommandResult], ticket: dict) -> list[FrozenBlock]:
    """Failures that named a frozen file, classified by whether the agent could
    have fixed them.

    Pure post-processing over named_paths: it runs nothing. Both callers already
    hold the results they need - approve from its probe, the session controller
    from the full_gate record it just wrote - so the classification costs no
    second gate run.
    """
    frozen = set(ticket.get("frozen") or [])
    scope = set(ticket.get("scope") or [])
    blocks = []
    for c in results:
        if c.ok:
            continue
        named_frozen = [rel for rel in c.named_paths if rel in frozen]
        if not named_frozen:
            continue
        blocks.append(
            FrozenBlock(
                command=c.command,
                exit_code=c.exit_code,
                frozen=named_frozen,
                fixable=[rel for rel in c.named_paths if rel in scope],
            )
        )
    return blocks


def run_full_gate_probe(root: Path, ticket: dict) -> list[CommandResult]:
    """Run every full_gate command at approve time, skipping nothing.

    The three-token prefix skip that used to live here was moved out to the
    warning that needs it. It must not touch this run: the command it skips is
    almost always the repo-wide suite (`python3 -m pytest -q` shares a prefix
    with any pytest acceptance command), which is precisely the command whose
    pre-existing failures the baseline exists to record. Skipping it produced a
    baseline that was silent about the only part of the gate that ratchets.
    """
    if not ticket.get("full_gate"):
        return []
    kills = ticket.get("kill_conditions") or {}
    return run_commands(
        root, list(ticket["full_gate"]),
        deny_network=(kills.get("network_access") == "deny"),
        flag_paths=declared_paths(ticket),
        timeout_s=command_timeout(ticket),
    )


def probe_full_gate(ticket: dict, results: list[CommandResult]) -> list[FrozenBlock]:
    """Failures landing in a frozen file, for the approve-time warning.

    ADVISORY ONLY. Before the implementation exists, "fails naming the frozen
    test" is precisely what red looks like, so nothing here can separate a gate
    no implementation could clear from a ticket that simply has not been built
    yet. The information to tell those apart does not exist at approve time, so
    this warns and never refuses. The dispositive check runs at promotion, where
    acceptance has passed and the same signal means only one thing.

    The three-token prefix skip lives here and only here. Its job is to keep the
    warning quiet: without it every well-formed ticket warns about its own
    acceptance command, and a warning that always fires is one nobody reads.
    Being wrong now costs a missed warning rather than a blocked approve, and
    the promotion check - which does not skip - covers the blind spot.
    """
    if not ticket.get("frozen"):
        return []
    skip = {_command_prefix(c) for c in ticket.get("acceptance") or []}
    kept = [c for c in results if _command_prefix(c.command) not in skip]
    return frozen_blocks(kept, ticket)


def build_baseline(root: Path, results: list[CommandResult]) -> dict:
    """The pre-existing failure set, as measured at approve time.

    This is the answer to the question a repo-wide full_gate cannot otherwise
    survive: on a codebase with any existing red, "did the agent break
    something" is not the same question as "is the suite green", and only the
    first one is the agent's responsibility. Without a baseline the gate asks
    the second, so no ticket ever promotes evidence and the failure is reported
    as the agent's.

    Recorded, not inferred: the commit and timestamp travel with the keys, so a
    record derived from this baseline can say what it was ratcheted against
    rather than leaving a reader to assume it was current.
    """
    return {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "commit": git(root, "rev-parse", "HEAD"),
        "commands": {
            c.command: {"exit_code": c.exit_code, "keys": c.failure_keys}
            for c in results
        },
    }


def baseline_growth(old: dict, new: dict) -> list[str]:
    """How a re-approval's baseline is WORSE than the one it would replace.

    A ratchet is only a ratchet if widening it is deliberate. Left alone,
    re-approving after the repo has picked up new failures silently folds them
    into "pre-existing", and from then on the gate certifies breakage it was
    built to catch - the coarse lint key makes this easy, since a new finding
    landing on a file and rule already in the baseline is invisible to a
    set comparison. Growth therefore needs --rebaseline; shrinking never does.
    """
    grown = []
    old_cmds = (old or {}).get("commands") or {}
    for cmd, entry in ((new or {}).get("commands") or {}).items():
        before = old_cmds.get(cmd)
        if before is None:
            continue  # a command that is new to the ticket has nothing to widen
        added = new_failure_keys(entry.get("keys"), before.get("keys"))
        if added:
            grown.append(f"{cmd!r} now also fails: {', '.join(added)}")
        elif added is None and before.get("keys") is not None:
            # Was comparable, is not any more. Adopting that silently retires
            # the ratchet on this command without saying so.
            grown.append(
                f"{cmd!r} no longer reports failures this gate can identify, "
                f"so it can no longer be ratcheted"
            )
    return grown


# --- commands --------------------------------------------------------------


def prove_red_or_die(root: Path, ticket: dict, commands: list[str]) -> list[dict]:
    """Run the acceptance commands before the work exists and require them to
    fail WITH TEETH. Returns the red-proof entries; refuses approval otherwise.

    Everything downstream rests on the frozen tests having teeth. Nothing
    checked that. A test that asserts nothing passes before the work exists,
    sails through the gate on the first iteration, and promotes evidence for an
    implementation nobody wrote - the freeze mechanism faithfully protecting a
    contract that says nothing.

    Redness alone was never that check, and every red proof this repo has taken
    shows it: a greenfield ticket's module does not exist at approve time, so its
    commands fail during collection, the test never runs, and no assertion in it
    is ever executed. What the lock recorded was that an import failed. So the
    bar the mutation proof already enforces is applied here too - a pytest FAILED
    at a node id inside one of the ticket's frozen files, per command, judged by
    `detected_node_ids` unchanged. In practice the author lands importable
    signature stubs in `scope` before approve, which turns `ERROR <file>` into
    `FAILED <file>::<test> - NotImplementedError`; a test that asserts nothing
    then goes green instead, and the all-pass refusal below catches it.

    Returns one {command, exit_code, detected_by} dict per failing command, in
    acceptance order, which cmd_approve writes into the lock verbatim. Detection
    is computed here, where the output is read, rather than recomputed at the
    lock site from a truncated output_tail - the same reason CommandResult
    already stores named_paths and failure_keys instead of re-deriving them.
    """
    kills = ticket.get("kill_conditions") or {}
    frozen = list(ticket.get("frozen") or [])
    red = run_commands(
        root, commands,
        deny_network=(kills.get("network_access") == "deny"),
        flag_paths=declared_paths(ticket),
        timeout_s=command_timeout(ticket),
    )
    # A command that was killed is not red. It produced no result at all, and
    # accepting it would lock in a "proof" that the test can fail on the
    # strength of a hang - the weakest possible evidence wearing the strongest
    # label. Refuse rather than record it.
    killed = [c for c in red if c.timed_out]
    if killed:
        die(
            "acceptance command(s) timed out during the red proof: "
            + "; ".join(f"{c.command!r} after {c.duration_s}s" for c in killed)
            + ". A killed command reported no result, so it is not evidence the "
            "test can fail. Fix the hang, or raise "
            "kill_conditions.command_timeout_s if the command is merely slow."
        )
    if all(c.ok for c in red):
        die(
            "acceptance commands already pass, so freezing them proves nothing: "
            "either they assert nothing, or the implementation already exists. "
            "Approval happens before the work. Re-run with --allow-passing only "
            "if you are deliberately re-approving a ticket already implemented."
        )

    # D16, the red tier's half of D8's rule. A linter goes red on a module that
    # does not exist whether or not any test asserts anything, so counting its
    # exit code as the proof reopens the vacuity hole for exactly the commands it
    # covered. Refused rather than exempted: every ticket in this repo pairs its
    # pytest commands with `ruff check .`, so an exemption keyed on the absence
    # of a pytest command is the vacuous case the rule exists to catch, wearing a
    # waiver.
    if not any(_supplies_detection(c) for c in commands):
        die(
            "no acceptance command can supply the red proof: "
            + "; ".join(repr(c) for c in commands)
            + ". The proof is a pytest FAILED at a node id in a frozen file - a "
            "non-zero exit from anything else says only that something went red, "
            "which a test asserting nothing produces just as readily."
        )

    # Per command, on the union of offenders. A FAILED reported by one command
    # does not discharge another: on a twelve-command ticket that would prove one
    # test has teeth and leave eleven unexamined, the shape D6 rejected for
    # `expects` and D7 for survivors. And an author who learns of the next
    # offender only on the next approve fixes twelve commands one approve at a
    # time, so all of them are named at once.
    offenders = [
        c for c in red
        if _supplies_detection(c.command) and not detected_node_ids(c.output_tail, frozen)
    ]
    if offenders:
        die(
            "these acceptance command(s) produced no pytest FAILED at a node id "
            "inside " + (", ".join(frozen) or "a frozen file") + ": "
            + "; ".join(f"[{c.exit_code}] {c.command!r}" for c in offenders)
            + ". ERROR is not FAILED: a test that asserts nothing never runs, so "
            "the worst it can produce is a collection error, and an import break "
            "turns the whole suite red while proving nothing about any assertion "
            "in it. Nor is a FAILED elsewhere - that is an assertion firing in a "
            "test this ticket does not freeze and cannot hold the agent to. Land "
            "importable stubs in `scope` so the frozen tests reach their "
            "assertions and fail there."
        )

    return [
        {
            "command": c.command,
            "exit_code": c.exit_code,
            # Empty for a non-pytest command, and recorded rather than omitted:
            # that is what says in the lock that the command went red and
            # supplied nothing.
            "detected_by": (
                detected_node_ids(c.output_tail, frozen)
                if _supplies_detection(c.command)
                else []
            ),
        }
        for c in red
        if not c.ok
    ]


# --- the mutation proof ------------------------------------------------------
#
# What makes a characterization ticket approvable. Such a test pins behaviour
# that already exists, so it passes on day one - passing is what makes it
# correct - and prove_red_or_die's bar cannot be met without lying. Weakening
# the bar is not available either: green at base plus green at HEAD plus a diff
# inside scope is satisfied by a test that asserts nothing.
#
# The separator is that a vacuous test never runs, so the worst it can produce
# is a pytest ERROR; only a test with an assertion in it can produce FAILED. So
# the ticket declares perturbations of the code it characterizes, and approval
# requires the frozen node ids it named to come back FAILED against each one -
# green where it pins, dead where it declared. Stronger than a red proof.


@dataclass
class Mutation:
    """One declared perturbation, and the frozen node ids it must trip.

    `expects` has no default: without it the entry only asks that something went
    red, and a mutation to the codec caught by the test asserting the output path
    would count - proving that test can fail for an unrelated reason.
    """

    command: str
    expects: list[str]


def mutation_entries(ticket: dict, allow_passing: bool = False) -> list[Mutation]:
    """The ticket's declared mutations. Pure: it runs nothing.

    Empty for an ordinary ticket, and that emptiness is the whole mode switch -
    a ticket that declares no mutations keeps the red proof it has always had.
    """
    raw = ticket.get("mutation") or []
    if not raw:
        return []
    if allow_passing:
        die(
            "--allow-passing was given for a ticket that declares mutations. That "
            "flag re-approves a ticket already implemented; here it would skip the "
            "one proof the block exists to take. Drop one or the other."
        )
    entries = []
    for i, item in enumerate(raw):
        command = item.get("command") if isinstance(item, dict) else None
        expects = item.get("expects") if isinstance(item, dict) else None
        if not isinstance(command, str) or not command.strip():
            die(f"mutation entry {i} needs a non-empty 'command' and 'expects': {item!r}")
        if not isinstance(expects, list) or not expects or not all(
            isinstance(e, str) and e.strip() for e in expects
        ):
            die(
                f"mutation entry {i} needs a non-empty 'expects' listing the frozen "
                f"node ids this mutation must trip: {item!r}. Without it the entry "
                f"only asks that something went red."
            )
        entries.append(Mutation(command=command, expects=list(expects)))
    return entries


def detected_node_ids(output: str, frozen: list[str]) -> list[str]:
    """Node ids one command's output reported FAILED inside a frozen file.

    Pure - it runs nothing, the shape frozen_blocks established, so the rules
    about what counts cost tests rather than mutation runs.

    ERROR is never detection, and that is the case the whole design rests on: a
    test that asserts nothing never runs, so the worst it can do is error, and an
    import break turns the whole suite red while proving nothing about any
    assertion in it. A FAILED outside the frozen files is not detection either;
    nothing else is under contract.

    Reads the output tail, where pytest's short summary lives. A run long enough
    to push a FAILED past TAIL_CHARS drops it from the detected set and refuses
    an approval that should have passed - the safe direction, and the only one
    available while CommandResult keeps the tail alone.
    """
    files = set(frozen)
    return sorted(
        {
            node
            for node in PYTEST_FAILED_RE.findall(output)
            if node.split("::", 1)[0] in files
        }
    )


def unfired_expectations(expects: list[str], detected: list[str]) -> list[str]:
    """Declared node ids that nothing in `detected` matches, in declared order.

    A detected id matches on equality, or when it is the expected id followed by
    '[' - so a base id is satisfied by its parametrised cases, while
    `<expected>_and_something_else` is a different test and not a match.

    Every entry must fire. Listing several plausible catchers and being right
    about one would leave unverified claims in the lock beside verified ones.
    """
    return [e for e in expects if not any(d == e or d.startswith(f"{e}[") for d in detected)]


def remove_worktree(root: Path, path: Path) -> None:
    """Delete a mutation worktree, forcibly. Raises CalledProcessError if git
    refuses.

    --force because a bare `git worktree remove` fails on a dirty tree, which is
    the state every mutation leaves: the cleanup would fail in exactly the case
    it exists for. A module-level name so the failure path can be tested without
    breaking git, the reason docker_network_internal is one.
    """
    subprocess.run(
        ["git", "-C", str(root), "worktree", "remove", "--force", str(path)],
        capture_output=True, text=True, check=True,
    )


@contextmanager
def mutation_worktree(root: Path):
    """A throwaway detached checkout of HEAD, removed on the way out.

    The mutation is destructive by design, so it never runs in the tree the user
    is working in. An orphaned worktree at HEAD with a mutation applied is a trap
    - discovered by an error message here, or by something confusing later - so a
    removal git refused refuses the approval.
    """
    path = root / ".edad" / "worktrees" / f"mutation-{uuid.uuid4().hex[:12]}"
    path.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "--detach", str(path), "HEAD")
    try:
        yield path
    finally:
        try:
            remove_worktree(root, path)
        except subprocess.CalledProcessError as e:
            die(
                f"could not remove the mutation worktree at {path}: git exited "
                f"{e.returncode}. It is a detached checkout of HEAD with a mutation "
                f"applied, so it must not be left behind."
            )


def mutation_touched_paths(worktree: Path) -> list[str]:
    """Repo-relative paths the mutation changed inside its worktree."""
    return [p for p in git(worktree, "diff", "--name-only").splitlines() if p]


def _git_answer(root: Path, *args: str) -> str | None:
    """git's stdout for `root`, or None when git could not answer at all.

    Tolerant where git_raw is strict, for the two questions the mutation proof
    asks before it has established anything about `root`: is a frozen path dirty,
    and what is HEAD. None means there is no repository here - which approve
    cannot reach, having resolved `root` from git in the first place.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def dirty_frozen_paths(root: Path, frozen: list[str]) -> list[str]:
    """Frozen paths that are untracked or modified in `root`."""
    if not frozen:
        return []
    out = _git_answer(root, "status", "--porcelain", "-uall", "--", *frozen)
    if out is None:
        return []
    return sorted(
        {line[3:].strip().split(" -> ")[-1] for line in out.splitlines() if len(line) > 3}
    )


def _supplies_detection(command: str) -> bool:
    """Whether this command could report a pytest FAILED at a node id.

    An exit-code fallback for everything else was rejected: a linter goes red
    under a mutation whether or not any test asserts anything, so counting that
    as detection reopens the vacuity hole for exactly the commands it covered. A
    non-pytest command still runs and must be green; its exit code is never proof.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return any(t == "pytest" or t.endswith("/pytest") for t in tokens)


def prove_mutation_or_die(root: Path, ticket: dict, commands: list[str]) -> dict:
    """Require every declared mutation to be caught by the node ids it named.

    The sibling of prove_red_or_die, for the ticket type that cannot produce a
    red: green at base first - a characterization test failing against the code
    it claims to characterize is simply wrong - then dead in the declared places
    under every mutation. Returns the evidence; refuses approval otherwise.
    """
    frozen = list(ticket.get("frozen") or [])
    dirty = dirty_frozen_paths(root, frozen)
    if dirty:
        die(
            "frozen file(s) are uncommitted: " + ", ".join(dirty) + ". The mutation "
            "is measured against committed bytes while the lock hashes what is on "
            "disk - a proof about text the contract does not cover. Commit them."
        )

    kills = ticket.get("kill_conditions") or {}
    deny_network = kills.get("network_access") == "deny"
    flag_paths = declared_paths(ticket)
    timeout_s = command_timeout(ticket)

    base = run_commands(
        root, commands,
        deny_network=deny_network, flag_paths=flag_paths, timeout_s=timeout_s,
    )
    not_green = [c for c in base if not c.ok]
    if not_green:
        die(
            "a mutation proof requires the acceptance commands to pass against the "
            "code they characterize, and these did not: "
            + "; ".join(f"[{c.exit_code}] {c.command!r}" for c in not_green)
            + ". A test that fails against the behaviour it claims to pin is wrong."
        )

    if not any(_supplies_detection(c) for c in commands):
        die(
            "no acceptance command can supply detection: "
            + "; ".join(repr(c) for c in commands)
            + ". Detection is a pytest FAILED at a declared node id - ERROR is not, "
            "and neither is a non-zero exit from anything else: both are produced "
            "just as readily by a test that asserts nothing."
        )

    proof = {
        "commit": (_git_answer(root, "rev-parse", "HEAD") or "").strip(),
        "green_at_base": [{"command": c.command, "exit_code": c.exit_code} for c in base],
        "mutations": [],
    }
    scope = list(ticket.get("scope") or [])

    for entry in mutation_entries(ticket):
        with mutation_worktree(root) as wt:
            applied = run_commands(
                wt, [entry.command],
                deny_network=deny_network, flag_paths=flag_paths, timeout_s=timeout_s,
            )[0]
            # Before any acceptance command runs here. A mutation that did not
            # apply leaves the worktree at HEAD, where the acceptance commands
            # pass - which would read as "undetected" and blame the test for the
            # mutation's own failure to run.
            if applied.timed_out or not applied.ok:
                die(
                    f"the mutation command did not apply: {entry.command!r} "
                    + (
                        f"was killed after {applied.duration_s}s"
                        if applied.timed_out
                        else f"exited {applied.exit_code}"
                    )
                    + ". Nothing was perturbed, so there is nothing to detect."
                )

            touched = mutation_touched_paths(wt)
            strays = [
                p for p in touched if not any(match_scope(pat, p) for pat in scope)
            ]
            if strays:
                die(
                    f"the mutation {entry.command!r} changed files outside the "
                    f"ticket's scope: " + ", ".join(strays) + ". `scope` names the "
                    "code the characterization pins, so a mutation reaching past it "
                    "measures something the ticket never claimed."
                )

            results = run_commands(
                wt, commands,
                deny_network=deny_network, flag_paths=flag_paths, timeout_s=timeout_s,
            )
            detected: set[str] = set()
            acceptance_command = None
            for c in results:
                found = detected_node_ids(c.output_tail, frozen)
                if found and acceptance_command is None:
                    acceptance_command = c.command
                detected.update(found)

            if not detected:
                die(
                    f"the mutation {entry.command!r} survived: no frozen test "
                    f"reported FAILED against it. These ran and missed it: "
                    + "; ".join(repr(c.command) for c in results)
                    + ". An approved blind spot is a licensed regression."
                )

            unfired = unfired_expectations(entry.expects, sorted(detected))
            if unfired:
                die(
                    f"the mutation {entry.command!r} did not trip the node id(s) it "
                    f"declared: " + ", ".join(unfired) + ". Detected instead: "
                    + ", ".join(sorted(detected))
                    + ". A mutation caught by some other test proves that test can "
                    "fail for an unrelated reason, not that the declared one has teeth."
                )

            proof["mutations"].append(
                {
                    "command": entry.command,
                    "touched": touched,
                    "expects": list(entry.expects),
                    "acceptance_command": acceptance_command,
                    "detected_by": sorted(detected),
                }
            )
    return proof


def refuse_silent_widening(root: Path, ticket_id: str, baseline: dict) -> None:
    prior = approval_meta(root, ticket_id).get("full_gate_baseline") or {}
    grown = baseline_growth(prior, baseline)
    if grown:
        die(
            "this would widen the full_gate baseline: "
            + "; ".join(grown)
            + ". Adopting that silently would make the gate treat newly broken "
            "things as pre-existing, which is the failure the baseline exists to "
            "catch. Fix them, or re-run with --rebaseline to record the wider "
            "baseline deliberately."
        )


def print_approval_proof(red: list[dict], mutation_proof: dict | None) -> None:
    """What this approval rests on. Exactly one of the three is true."""
    if mutation_proof:
        muts = mutation_proof.get("mutations") or []
        print(f"  mutation proof ({len(muts)} mutation(s), each caught where declared):")
        for m in muts:
            print(f"    {m['command']}")
            print(f"      -> {', '.join(m['detected_by'])}  ({m['acceptance_command']})")
    elif red:
        print("  red proof (these failed before the work existed):")
        for e in red:
            # The node ids, not just the exit code: which frozen test was proven
            # to have teeth is the whole claim, and a command that supplied none
            # says so here rather than hiding behind a sibling that did.
            nodes = e.get("detected_by") or []
            detail = ", ".join(nodes) if nodes else "supplied no FAILED"
            print(f"    [{e['exit_code']}] {e['command']}")
            print(f"      -> {detail}")
    else:
        print("  ! no red proof recorded (--allow-passing)")


def print_baseline(baseline: dict) -> None:
    pre = {
        cmd: e["keys"]
        for cmd, e in baseline["commands"].items()
        if e["exit_code"] != 0
    }
    if not pre:
        print("  full_gate baseline: clean")
        return
    for cmd, keys in pre.items():
        n = "unidentifiable" if keys is None else str(sum(keys.values()))
        print(f"  full_gate baseline: {n} pre-existing failure(s) in {cmd}")
        if keys is None:
            print("    ! cannot be ratcheted; this command stays all-or-nothing")


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
    # Reads the ticket and validates it; this is also what refuses
    # --allow-passing alongside a mutation block, before anything has run.
    mutations = mutation_entries(ticket, args.allow_passing)
    # A declared mutation block selects the proof. The two are alternatives, not
    # a fallback: a characterization test is green at base by construction, so
    # asking it for a red first would refuse every correct one.
    prove_red = bool(commands) and not mutations and not args.allow_passing

    # Both checks below execute commands, and both misread a broken toolchain:
    # a missing pytest fails for the wrong reason, which reads as red proof the
    # ticket has not earned and as a full_gate failure nobody can fix.
    if prove_red or mutations or ticket.get("full_gate"):
        problems = gate_toolchain_problems(root)
        if problems:
            die(
                "the gate's toolchain does not match requirements-gate.txt: "
                + "; ".join(problems) + ". A missing tool fails for the wrong reason "
                "and would read as red. Install the pins: "
                "pip install -r requirements-gate.txt"
            )

    probe = run_full_gate_probe(root, ticket)
    baseline = build_baseline(root, probe) if probe else None
    if baseline and not args.rebaseline:
        refuse_silent_widening(root, ticket["id"], baseline)

    # Advisory, not a refusal: at this moment a full_gate failure inside the
    # frozen test is indistinguishable from the red this ticket is supposed to
    # be in. Blocking on it refuses well-formed tickets; the session makes the
    # same call at promotion, where acceptance is green and the answer is real.
    for block in probe_full_gate(ticket, probe):
        warn(
            "full_gate may be unwinnable: " + block.describe() + ". The agent may "
            "not edit a frozen file, so if this failure survives the implementation "
            "no session can clear it. Not blocking approval - before the work exists "
            "this looks the same as the expected red. The session re-checks at "
            "promotion, where the answer is decidable."
        )

    mutation_proof = prove_mutation_or_die(root, ticket, commands) if mutations else None
    red = prove_red_or_die(root, ticket, commands) if prove_red else []

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
        # Written verbatim as prove_red_or_die measured it, one entry per failing
        # command, each carrying the frozen node ids that command reported
        # FAILED. A bare {command, exit_code} shape loses the FAILED/ERROR
        # distinction the whole design rests on - the ground this block already
        # rejects it on for mutation_proof.
        "red_proof": list(red) or None,
        # Records the node ids, not merely that the gate was satisfied: a
        # {command, exit_code} shape mirroring red_proof would lose the
        # FAILED/ERROR distinction the whole design rests on.
        "mutation_proof": mutation_proof,
        # What was already failing before the work began. The session subtracts
        # this at promotion so a pre-existing failure is attributed to the repo
        # rather than to the agent.
        "full_gate_baseline": baseline,
    }
    out = root / ".edad" / "hashes" / f"{ticket['id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(lock, indent=2) + "\n")
    print(f"approved {ticket['id']}: {len(hashes)} frozen file(s)")
    for rel, h in hashes.items():
        print(f"  {h[:12]}  {rel}")
    print_approval_proof(red, mutation_proof)
    if baseline:
        print_baseline(baseline)
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
        commands_ok=False,
        scope_enforced=bool(kills.get("diff_touches_outside_scope", True)),
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
    rec.mutation_proof = meta.get("mutation_proof")

    rec.freeze_ok, freeze_problems = check_freeze(root, ticket)
    rec.violations += freeze_problems

    rec.changed_files = changed_files(root, base_ref)
    # Measure unconditionally. The policy decides whether this counts against
    # the verdict, never whether it is recorded: an unenforced violation is
    # still a fact about this diff, and a reader of the record is entitled to
    # it. `violations` stays the list of reasons the run FAILED, so an
    # unenforced stray belongs in scope_violations alone.
    _, rec.scope_violations = check_scope(ticket, rec.changed_files)
    if rec.scope_enforced:
        rec.violations += rec.scope_violations

    # A tampered acceptance test invalidates the run. Do not execute it.
    if not rec.freeze_ok:
        rec.commands_ok = False
    else:
        rec.commands = run_commands(
            root, commands,
            deny_network=(kills.get("network_access") == "deny"),
            flag_paths=declared_paths(ticket),
            timeout_s=command_timeout(ticket),
        )
        rec.commands_ok = all(c.ok for c in rec.commands)
        rec.violations += [
            (
                f"command timed out after {c.duration_s}s (no result): {c.command}"
                if c.timed_out
                else f"command failed ({c.exit_code}): {c.command}"
            )
            for c in rec.commands
            if not c.ok
        ]
        if gate_name == "full_gate" and not rec.commands_ok:
            apply_ratchet(rec, meta.get("full_gate_baseline") or {})

    return rec


def apply_ratchet(rec: Record, baseline: dict) -> None:
    """Subtract the approve-time baseline from this run's failures.

    Only meaningful for full_gate, which runs repo-wide: on a codebase carrying
    any existing red, "is the suite green" and "did this change break something"
    are different questions, and only the second one is the agent's. Without
    this the gate asks the first, attributes the answer to the agent, and
    promotes evidence for nothing - ever.
    """
    rec.baseline_commit = baseline.get("commit")
    entries = baseline.get("commands") or {}
    for c in rec.commands:
        if c.ok:
            continue
        before = entries.get(c.command)
        added = new_failure_keys(c.failure_keys, before.get("keys")) if before else None
        if added is None:
            rec.uncomparable_failures.append(c.command)
        else:
            rec.new_failures[c.command] = added


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
    payload["scope_ok"] = rec.scope_ok
    payload["passed"] = rec.passed
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def verdict_line(rec: Record) -> str:
    """The last line of a report.

    Three outcomes, not two. A run that failed only in ways the baseline already
    had is not a FAIL - nothing here is the ticket's doing - but it is not a
    plain PASS either, because the suite was not green. Saying "PASS" flat would
    let a reader conclude something the gate did not measure; the qualifier is
    the whole point, and the baseline lines printed above it say against what.
    """
    if rec.passed:
        return "PASS"
    if rec.passed_modulo_baseline:
        return "PASS (modulo the approved baseline - nothing failing is new)"
    return "FAIL"


def report(rec: Record) -> None:
    def mark(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    print(f"\n{rec.ticket} @ {rec.commit[:8]}  [{rec.gate}]")
    print(f"  freeze   {mark(rec.freeze_ok)}")
    scope_note = "" if rec.scope_enforced else "  (measured, not enforced)"
    print(
        f"  scope    {mark(rec.scope_ok)}  "
        f"({len(rec.changed_files)} file(s) changed){scope_note}"
    )
    if not rec.scope_ok and not rec.scope_enforced:
        # The stray never reaches `violations` when the ticket switched the
        # kill condition off, so without this the run prints a clean scope line
        # and the reader never learns the diff left its declared boundary.
        for v in rec.scope_violations:
            print(f"  ~ {v} (diff_touches_outside_scope is off; not counted)")
    print(f"  commands {mark(rec.commands_ok)}")
    for c in rec.commands:
        killed = "  KILLED (no result)" if c.timed_out else ""
        print(f"    [{c.exit_code}] {c.duration_s}s  {c.command}{killed}")
    for v in rec.violations:
        print(f"  ! {v}")
    if rec.gate == "full_gate" and not rec.commands_ok:
        base = (rec.baseline_commit or "")[:8] or "none recorded"
        print(f"  baseline {base}")
        for c, keys in sorted(rec.new_failures.items()):
            print(f"    {'new: ' + ', '.join(keys) if keys else 'nothing new'}  ({c})")
        for c in rec.uncomparable_failures:
            print(f"    ! not comparable against the baseline: {c}")
    if rec.decisions:
        print(f"  decisions {', '.join(rec.decisions)}")
    if rec.approved and not rec.ticket_verified:
        print("  ! approval lock predates ticket hashing: the frozen tests were "
              "verified, the ticket's own fields were not")
    report_proof(rec)
    print(f"  => {verdict_line(rec)}\n")


def report_proof(rec: Record) -> None:
    """What the approval this record cites actually established.

    Three tiers, because there are three claims. A mutation proof is not the
    --allow-passing case and must never print as one.
    """
    if rec.mutation_proof:
        # The counts are disclosure: four mutations all caught by one assertion
        # must not read identically to four caught by four. Nothing requires the
        # declared mutations to span the behaviour the test claims to pin, and
        # nothing here can check that - so a narrow proof passes but cannot look
        # wide.
        muts = rec.mutation_proof.get("mutations") or []
        distinct = {n for m in muts for n in (m.get("detected_by") or [])}
        print(
            f"  mutation proof at approval: {len(muts)} mutation(s) caught by "
            f"{len(distinct)} distinct node id(s)"
        )
    elif rec.red_proof:
        # On the presence of the stored field, never on the exit code. The lock
        # is what records that anything was measured, so an old-shape entry
        # carrying exit 1 - which is what a real FAILED exits with - still reads
        # as pre-amendment. Inferring would classify the five existing locks for
        # free while inventing a claim their runs never recorded.
        measured = all(
            isinstance(e, dict) and "detected_by" in e for e in rec.red_proof
        )
        if measured:
            nodes = {n for e in rec.red_proof for n in (e.get("detected_by") or [])}
            print(
                f"  red proof at approval: {len(rec.red_proof)} command(s) failed, "
                f"{len(nodes)} frozen node id(s) reported FAILED"
            )
        else:
            print(
                f"  ! pre-amendment red proof at approval: {len(rec.red_proof)} "
                f"command(s) went red, but nothing recorded which frozen node ids "
                f"reported FAILED - the tests' teeth were never verified, and a "
                f"non-zero exit here may have been a collection error"
            )
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


def main() -> int:
    p = argparse.ArgumentParser(prog="edad.gate")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("approve", help="record hashes of the ticket's frozen files")
    a.add_argument("ticket")
    a.add_argument("--allow-passing", action="store_true",
                   help="approve even if the acceptance commands already pass")
    a.add_argument("--rebaseline", action="store_true",
                   help="deliberately widen the full_gate baseline to include failures "
                        "that appeared since the last approval")
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

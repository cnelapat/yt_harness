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
    python3 -m edad.session run T001 --sandbox docker --network edad-fixtures
                                                       # container on that network,
                                                       # reaching the model only via
                                                       # the egress proxy; refused
                                                       # unless preflight measured it
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

# By name into this module: cmd_run's lifecycle is tested with the ensure and
# remove replaced at `session.`, not at `egress.`.
from edad.egress import (
    EgressError,
    ensure_egress_proxy,
    proxy_url,
    remove_egress_proxy,
)
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
TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
# The permit probe is one trivial model turn (measured 4s) and, through a dead
# proxy, 20s of the CLI's own retries. Well past both: a probe that is still
# running at this point is a configuration nobody measured, and is refused.
PERMIT_PROBE_TIMEOUT_S = 180
PERMIT_PROBE_PROMPT = "Reply with the single word: ok"
# The checks a --dry-run skips, by name, so cmd_run can print them.
DRY_RUN_SKIPS = ["oauth token", "proxy refuses", "proxy permits"]
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


def preflight(  # noqa: PLR0913  # the run's five knobs, passed through; not five jobs
    root: Path, ticket: dict, sandbox: str, dry_run: bool,
    network: str | None = None, image: str = DEFAULT_IMAGE,
) -> list[str]:
    """Refuse to start rather than fail expensively halfway through.

    Returns the names of the network checks that were skipped (see
    validate_network): cmd_run prints them, and has no other way to learn them.
    """
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
    return validate_network(sandbox, network, image, dry_run)


def docker_network_internal(name: str) -> str | None:
    """Docker's own answer to whether `name` is an internal network: the
    stripped `{{.Internal}}` output, or None when docker did not answer.

    Every way of not answering collapses to None on purpose - no such network,
    daemon not running, docker not installed. The caller does not act on the
    distinction: what it needs to know is whether the isolation was measured,
    and an unmeasured network is refused whatever the reason.

    A module-level name so the refusal logic can be tested against each answer
    without a daemon; every other shell-out in this tier follows the same shape.
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


def probe_container(network: str, image: str, script: str,
                    timeout: int = 60) -> tuple[int, str] | None:
    """`(returncode, stdout)` of `python3 -c script` in a throwaway container
    on `network`, or None when the container could not be run at all."""
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm", "--network", network, image, "python3", "-c", script],
            capture_output=True, text=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.returncode, proc.stdout.strip()


# Exit codes the probe scripts use to answer, distinct from anything docker or a
# missing python3 would produce (125-127), so those collapse to "not measured".
PROBE_YES = 0
PROBE_NO = 3

EGRESS_PROBE = """\
import socket, sys
s = socket.socket()
s.settimeout(5)
try:
    s.connect(("1.1.1.1", 443))
except OSError:
    sys.exit(3)
sys.exit(0)
"""

# Prints the status line of the proxy's reply and exits 0; exits 3 when the
# proxy could not be reached or did not answer.
REFUSE_PROBE = """\
import socket, sys
from urllib.parse import urlsplit
u = urlsplit(%r)
try:
    s = socket.create_connection((u.hostname, u.port), timeout=5)
    s.sendall(b"CONNECT example.com:443 HTTP/1.1\\r\\nHost: example.com:443\\r\\n\\r\\n")
    reply = s.recv(1024)
except OSError:
    sys.exit(3)
if not reply:
    sys.exit(3)
print(reply.split(b"\\r\\n", 1)[0].decode(errors="replace"))
"""


def docker_network_egress(network: str, image: str) -> bool | None:
    """D11's probe: whether an unconfigured container on `network` reaches
    the internet. True reached, False did not, None the probe could not be run.

    `Internal: true` no longer backs the claim on its own - a forwarding
    process on the network is egress whatever docker calls the network - so a
    throwaway container tries to reach out. A raw-IP TCP connect, no DNS
    anywhere: an internal net has no resolver, and the probe must be instant
    in both directions (measured: `Network is unreachable` in 0.00s).
    """
    answer = probe_container(network, image, EGRESS_PROBE)
    if answer is None:
        return None
    code, _ = answer
    if code == PROBE_YES:
        return True
    if code == PROBE_NO:
        return False
    return None


def proxy_refuses(network: str, image: str) -> bool | None:
    """D16's refusal half: whether the proxy on `network` answers a CONNECT to
    a non-allowlisted host with anything but 200. None when it could not be
    probed - no proxy to connect to, or no container to connect from.

    Asked from a container on the network, because that is where the proxy's
    name resolves and where the agent will be asking from.
    """
    answer = probe_container(network, image, REFUSE_PROBE % proxy_url(network))
    if answer is None:
        return None
    code, status_line = answer
    if code != PROBE_YES:
        return None
    parts = status_line.split()
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        return None
    return parts[1] != "200"


def permit_probe_argv(network: str, image: str) -> list[str]:
    """The `claude -p --max-turns 1` run that proves the proxy permits the
    model API with the token the agent will hold. Pure.

    One call proves three things - the proxy permits the API, the token is
    valid, and it is the token the agent will hold - only because the probe is
    the agent's own client, in the agent's own image, on the named network,
    with the token crossing the way it will cross for the agent: the bare
    `-e NAME` form, so its value is never an element of this list.
    """
    url = proxy_url(network)
    return [
        "docker", "run", "--rm",
        "--network", network,
        "-e", TOKEN_VAR,
        "-e", f"HTTPS_PROXY={url}",
        "-e", f"HTTP_PROXY={url}",
        "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
        image,
        "claude", "-p", "--max-turns", "1", PERMIT_PROBE_PROMPT,
    ]


def proxy_permits(network: str, image: str) -> tuple[int, str] | None:
    """D16's permit half: `(exit code, output tail)` of the permit probe, or
    None when it could not be started or did not finish.

    Not a bool, because the tail is what tells the refusals apart: a dead
    proxy (403, 20s of retries) and a bad token (401, 3s) print the same
    `Failed to authenticate` prefix and differ only in the status. The
    environment is inherited - that is how the token crosses.
    """
    try:
        proc = subprocess.run(
            permit_probe_argv(network, image), capture_output=True, text=True,
            check=False, timeout=PERMIT_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.returncode, (proc.stdout + proc.stderr)[-AGENT_TAIL:]


def validate_network(  # noqa: PLR0912  # one refusal per branch, cheapest first; the order is pinned
    sandbox: str, network: str | None, image: str = DEFAULT_IMAGE, dry_run: bool = False,
) -> list[str]:
    """Refuse a docker-tier run the harness cannot back with its own measurement.

    Every docker-tier refusal lives here, so the queue calling it once at plan
    time and the session calling it from preflight get the same checks from one
    function. Cheapest first, and each one's message is about its own cause,
    because every way the tier can be misconfigured - no route to the model, a
    dead proxy, a stale image, an expired token, a token never exported -
    otherwise surfaces as the same 15-retry storm at 3am.

    The default tier asserts nothing, so it asks nothing: probing on every run
    would let an unrelated docker problem refuse sessions that never needed
    docker. Once a name is given the prompt is going to tell the agent its only
    egress is the model API, and network_rule's rule applies - the claim is only
    allowed because this measured it: docker calls the network internal, a bare
    container on it reaches nothing, the proxy refuses a host off the allowlist
    and `claude -p` itself gets through it with the token the agent will hold.

    Returns the names of the checks it skipped: empty, unless `dry_run`. A
    dry-run exists to print the prompt, and the proxy probes would start a
    container and spend a model turn to do it, so it stops after the isolation
    check - and says so, so nobody is told a network was validated when half of
    the validation did not happen.
    """
    if network is None:
        if sandbox == "docker":
            raise Abort(
                "--sandbox docker without --network runs the container with "
                "--network none, where the CLI cannot reach the model at all: that "
                "branch can never run a real agent, and its symptom is a retry storm "
                "at the agent timeout. Name an internal network with --network "
                "(create one with: docker network create --internal <name>)."
            )
        return []
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

    # Internal is docker's word; egress is the measurement. A forwarding
    # process on the network is a route out whatever the network is called.
    egress = docker_network_egress(network, image)
    if egress is None:
        raise Abort(
            f"the egress probe could not be run on network {network!r} (image "
            f"{image!r} missing, or docker could not start a container on it). "
            "Its egress was not measured, so its isolation will not be promised "
            "to the agent."
        )
    if egress:
        raise Abort(
            f"network {network!r} has egress: an unconfigured container on it "
            "reached the internet, whatever docker calls the network. Something on "
            "it is forwarding. Attaching to it would put an isolation claim in "
            "every prompt that nothing enforces."
        )
    if dry_run:
        return list(DRY_RUN_SKIPS)

    # The token gates only the permit probe, so it sits beside it rather than
    # among the plain refusals: `docker run -e VAR` with VAR unset passes
    # nothing and raises no error, and 'unset' deserves a better message than
    # a failed API call - the variable, and the command that mints it.
    if not os.environ.get(TOKEN_VAR):
        raise Abort(
            f"{TOKEN_VAR} is not set. The agent container gets its credential only "
            "through this variable, and docker passes an unset -e silently, so the "
            "agent would start with no credential and no warning. Mint one with "
            f"'claude setup-token' and export {TOKEN_VAR}."
        )

    # The proxy, both directions. Refusal first: it is cheap, and the permit
    # probe spends a model turn.
    refuses = proxy_refuses(network, image)
    if refuses is None:
        raise Abort(
            f"the egress proxy for network {network!r} could not be probed from a "
            f"container on it (no proxy at {proxy_url(network)}, or no container to "
            "ask from). A boundary nobody measured is not promised."
        )
    if not refuses:
        raise Abort(
            f"the egress proxy for network {network!r} permitted a CONNECT to a "
            "host outside its allowlist. That is a hole in the boundary, not a "
            "tier; rebuild the proxy image and re-run."
        )
    permitted = proxy_permits(network, image)
    if permitted is None:
        raise Abort(
            f"the permit probe ('claude -p' through the egress proxy on network "
            f"{network!r}) could not be run, or did not finish within "
            f"{PERMIT_PROBE_TIMEOUT_S}s. Whether the proxy permits the model API "
            "was not measured."
        )
    code, tail = permitted
    if code != 0:
        raise Abort(
            f"'claude -p' through the egress proxy on network {network!r} exited "
            f"{code}: either the proxy does not permit the model API or the token "
            "is not valid, and its output says which (a 403 is the proxy, a 401 "
            f"the token):\n{tail}"
        )
    return []


def deny_enforcement(ticket: dict, sandbox: str) -> dict[str, str]:
    """Which half of `network_access: deny` this run enforces. Pure.

    Two claims, not one. On the agent container it is mechanical under docker
    and a sentence in a prompt otherwise. On the verifier it is EDAD_NETWORK=deny,
    a string a cooperating suite may honour, and nothing more (D8) - so it is
    always advisory. The session log carries the answer per run, so nobody
    reads 'deny' in a ticket and assumes both.
    """
    requested = (ticket.get("kill_conditions") or {}).get("network_access") == "deny"
    if not requested:
        return {"agent": "not requested", "verifier": "not requested"}
    return {
        "agent": "enforced" if sandbox == "docker" else "advisory",
        "verifier": "advisory",
    }


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
    network, so "--network none" would be false; and "no internet egress" would
    be a lie by omission, because there is one route out - the model API, via
    the proxy - and an agent told there is none will not understand why the
    model answers. So the sentence says what validate_network has just
    measured: the services on the network, the one route out, and nothing else.
    """
    if sandbox == "docker" and network is not None:
        lines = [
            f"You are attached to the docker network {network}: services on it are",
            "reachable by container name (for example a database at its container",
            "name, on its own port). Your only egress is the model API, via the",
            "egress proxy on that network; nothing else on the internet is",
            "reachable. The network is internal and the proxy's allowlist was",
            "measured before the run started. Do not attempt installs or downloads.",
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


def allowed_tools(ticket: dict) -> list[str]:
    """The host tier's `--allowedTools` patterns, from the ticket's own gate commands (D19)."""
    return []


def permissions(sandbox: str, yolo: bool, ticket: dict) -> dict:
    """What the agent could do this run, for the session log (D19)."""
    return {"mode": "", "allowed": None}


def agent_argv(  # noqa: PLR0913  # a pure argv builder: seven independent inputs, not seven jobs
    prompt: str, workdir: Path, sandbox: str, image: str, yolo: bool,
    network: str | None = None, allowed: list[str] | None = None,
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

    On a network the container is pointed at the egress proxy - both HTTPS_PROXY
    and HTTP_PROXY, because the CLI honours the first and a stray plain-HTTP call
    would otherwise try to leave directly - and nonessential traffic is off, which
    drops the telemetry host entirely and is what makes the allowlist one entry.
    Set for the container, not exported here: in the harness's own environment it
    would silence telemetry for the operator's interactive CLI too.

    The no-network docker argv is unchanged. validate_network refuses that
    combination one step earlier; this builder stays pure.
    """
    override = os.environ.get("EDAD_AGENT_CMD")
    base = shlex.split(override) if override else ["claude", "-p"]
    perm = ["--dangerously-skip-permissions"] if yolo else ["--permission-mode", "acceptEdits"]
    inner = [*base, *perm, prompt]

    if sandbox != "docker":
        return inner
    egress: list[str] = []
    if network is not None:
        url = proxy_url(network)
        egress = [
            "-e", f"HTTPS_PROXY={url}",
            "-e", f"HTTP_PROXY={url}",
            "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
        ]
    return [
        "docker", "run", "--rm",
        # No name: the deny in kill_conditions, made real. A name: a network
        # docker itself called internal, which is the same deny one tier wider.
        "--network", network if network is not None else "none",
        "-v", f"{workdir}:/work",
        "-w", "/work",
        # The bare -e NAME form: the value crosses from the environment and is
        # never an element of this list, so it reaches no process listing or log.
        "-e", TOKEN_VAR,
        *egress,
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
    network: str | None = None
    network_access: dict = field(default_factory=dict)
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


def cmd_run(args) -> int:
    """The proxy's lifecycle around the session (D17, the session half).

    Ensure before preflight, because the proxy must exist before
    validate_network can probe it; remove in a `finally` around everything that
    follows, because a refusal for any later reason - no lock, dirty tree - has
    already created a container that a teardown at the end of the session would
    never reach. Only if this call created it: a child of the queue finds the
    queue's proxy present and must leave it for the sessions after it. A
    dry-run starts no proxy, since it runs none of the checks that need one.
    """
    root = repo_root()
    ticket = load_ticket(root, args.ticket)
    created = False
    if args.sandbox == "docker" and args.network is not None and not args.dry_run:
        try:
            created = ensure_egress_proxy(args.network)
        except EgressError as e:
            raise Abort(f"the egress proxy for network {args.network!r} could not "
                        f"be ensured: {e}") from e
    try:
        return run_session(root, ticket, args)
    finally:
        if created:
            remove_egress_proxy(args.network)


def run_session(root: Path, ticket: dict, args) -> int:  # noqa: PLR0915  # linear driver; splitting hides the flow
    skipped = preflight(root, ticket, args.sandbox, args.dry_run, args.network, args.image)

    base = git(root, "rev-parse", "HEAD")
    wt, branch = make_worktree(root, ticket["id"], base)
    log = SessionLog(
        ticket=ticket["id"],
        started_at=datetime.now(timezone.utc).isoformat(),
        base_commit=base,
        branch=branch,
        sandbox=args.sandbox,
        network=args.network,
        network_access=deny_enforcement(ticket, args.sandbox),
    )

    prompt = initial_prompt(ticket, args.sandbox, args.network)
    if args.dry_run:
        print(prompt)
        # Say what was not checked, or the operator reads a printed prompt as a
        # validated network when half of the validation did not happen.
        unchecked = f"; skipped checks: {', '.join(skipped)}" if skipped else ""
        print(f"\n[dry-run] worktree {wt} on {branch}; no agent invoked{unchecked}")
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

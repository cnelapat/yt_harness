"""
FROZEN ACCEPTANCE TEST — ticket T017.

The agent's permissions, one rule per tier (D19). Found from the session logs
of the tier's first night: `--permission-mode acceptEdits` under `claude -p`
has nobody to answer a prompt, so every Bash call is denied, and every ticket
to date was implemented by an agent that could not run a test. Under docker
the container is the boundary, so the CLI bypasses permissions and the image
must not run as root (the CLI refuses the flag there). On the host there is
no boundary, so the agent is allowed exactly the ticket's own gate commands.

Everything here is pure: argv builders, a pattern builder, a log field, and a
static read of the Dockerfile. Nothing shells out. The live half — the rebuilt
image accepting the flag as its user — is `docker_tests/test_properties.py`,
hand-run after the image is rebuilt.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from edad.session import SessionLog, agent_argv, allowed_tools, permissions

IMAGE = "edad-agent:latest"
WORKDIR = Path("/work/T017")
NETWORK = "edad-fixtures"
PROMPT = "do the work"

BYPASS = "--dangerously-skip-permissions"

TICKET = {
    "id": "T1",
    "acceptance": [
        "python3 -m pytest tests/test_a.py::test_one -q",
        "python3 -m pytest tests/test_a.py::test_two -q",
        "ruff check edad/a.py",
    ],
    "full_gate": [
        "python3 -m pytest -q",
        "ruff check .",
        "ruff check .",                                   # a duplicate, on purpose
    ],
}

EXPECTED_ALLOWED = [
    "Bash(python3 -m pytest tests/test_a.py::test_one -q)",
    "Bash(python3 -m pytest:*)",
    "Bash(python3 -m pytest tests/test_a.py::test_two -q)",
    "Bash(ruff check edad/a.py)",
    "Bash(python3 -m pytest -q)",
    "Bash(ruff check .)",
]


def flag_value(argv, flag):
    return argv[argv.index(flag) + 1]


# --- the docker tier ----------------------------------------------------------


def test_docker_bypasses_permissions_whatever_yolo_says():
    """Inside the container the boundary is the container: model-API-only
    egress, the worktree as the only mount, a `.git` that points at a host
    path. `acceptEdits` there was not a safeguard but a crippled agent, so
    bypass is the rule and not an opt-in - `--yolo` changes nothing."""
    for yolo in (False, True):
        for network in (None, NETWORK):
            argv = agent_argv(PROMPT, WORKDIR, "docker", IMAGE, yolo, network=network)
            inner = argv[argv.index(IMAGE) + 1:]
            assert BYPASS in inner, (yolo, network)
            assert "--permission-mode" not in inner, (yolo, network)
            assert "--allowedTools" not in inner, (yolo, network)
            assert PROMPT in inner


# --- the host tier --------------------------------------------------------------


def test_the_host_tier_accepts_edits_and_allows_the_gate_commands_with_the_prompt_first():
    """No boundary on the host, so no bypass: edits are accepted and the
    commands allowed are exactly the ticket's own, comma-joined into one
    `--allowedTools`. The prompt comes first because the flag is variadic -
    measured: with the prompt after it, the CLI reports no prompt was given."""
    allowed = allowed_tools(TICKET)
    argv = agent_argv(PROMPT, WORKDIR, "none", IMAGE, False, allowed=allowed)

    assert argv[:2] == ["claude", "-p"]
    assert BYPASS not in argv
    assert flag_value(argv, "--permission-mode") == "acceptEdits"
    assert "--allowedTools" in argv
    assert flag_value(argv, "--allowedTools") == ",".join(EXPECTED_ALLOWED)
    assert argv.index(PROMPT) < argv.index("--allowedTools")
    assert argv.count("--allowedTools") == 1


def test_yolo_on_the_host_bypasses():
    """The one place `--yolo` still means something: an explicit, per-run
    opt-in to bypass where there is no boundary. Then no allowlist either -
    a bypass with an allowlist beside it would claim a restriction it does
    not enforce."""
    argv = agent_argv(PROMPT, WORKDIR, "none", IMAGE, True, allowed=allowed_tools(TICKET))

    assert BYPASS in argv
    assert "--permission-mode" not in argv
    assert "--allowedTools" not in argv
    assert PROMPT in argv
    # And the log says so: an operator reading "allowlist" for a --yolo run
    # would be reading a restriction that was not there.
    assert permissions("none", True, TICKET)["mode"] == "bypass"


def test_allowed_tools_names_each_gate_command_once_and_a_pytest_prefix():
    """Each acceptance and full_gate command exactly, in order of first
    appearance, once; and beside each pytest command the prefix through
    `pytest` with `:*`, so the agent can run one node id. Nothing else: no
    bare `python3`, no `ruff` prefix - a listed command is what it may run."""
    assert allowed_tools(TICKET) == EXPECTED_ALLOWED
    assert allowed_tools({"id": "T2"}) == []
    assert allowed_tools({"id": "T3", "acceptance": ["pytest tests/t.py::x"]}) == [
        "Bash(pytest tests/t.py::x)", "Bash(pytest:*)",
    ]


# --- what the log says ------------------------------------------------------------


def test_permissions_says_what_the_agent_could_do_per_tier():
    """Pure. `mode` is `bypass` or `allowlist`; `allowed` is the pattern list
    under an allowlist and None under bypass, because bypass means "everything"
    and an empty list would read as "nothing"."""
    assert permissions("docker", False, TICKET) == {"mode": "bypass", "allowed": None}
    assert permissions("docker", True, TICKET) == {"mode": "bypass", "allowed": None}
    assert permissions("none", True, TICKET) == {"mode": "bypass", "allowed": None}
    assert permissions("none", False, TICKET) == {
        "mode": "allowlist", "allowed": EXPECTED_ALLOWED,
    }


def test_the_session_log_records_permissions_beside_network_access():
    """A log already says where the agent could reach (`network_access`);
    now it says what it could do, right beside it, and defaults to "nothing
    recorded" rather than omitting the field."""
    log = asdict(SessionLog(ticket="T1", started_at="now", base_commit="0" * 40,
                            branch="edad/t1", sandbox="docker"))
    keys = list(log)
    assert keys[keys.index("network_access") + 1] == "permissions"
    assert log["permissions"] == {}


# --- the image ----------------------------------------------------------------------


def test_the_agent_image_does_not_run_as_root():
    """The CLI refuses `--dangerously-skip-permissions` as root ("cannot be
    used with root/sudo privileges"), measured against the built image. So
    the last USER instruction in Dockerfile.agent must name a non-root user,
    and it must come after the installs that need root. Static; the rebuilt
    image accepting the flag is the hand-run docker property test."""
    lines = Path("Dockerfile.agent").read_text().splitlines()
    users = [i for i, line in enumerate(lines) if line.split()[:1] == ["USER"]]
    assert users, "Dockerfile.agent has no USER instruction"
    user = lines[users[-1]].split()[1]
    assert user not in ("root", "0"), user
    installs = [i for i, line in enumerate(lines)
                if line.startswith("RUN") and ("apt-get" in line or "install" in line)]
    assert installs and users[-1] > max(installs), "USER must come after the installs"

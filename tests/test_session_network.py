"""
FROZEN ACCEPTANCE TEST — tickets T002 and T016.

Authored at the approve step, before implementation exists. The build agent may
read and run this file. It may NOT modify it. The gate runner verifies the file
hash before every run; a mismatch is a hard fail and aborts the session.

Run: pytest tests/test_session_network.py -q

Nothing here talks to docker. The point of the tier is a promise the harness has
measured rather than repeated, so what needs proving is the measuring, the
refusing and the sentence the agent is told — and a test that needed a running
daemon would be skipped on precisely the machines where a wrong answer is
cheapest to ship.

T016 (from "the egress probe" down) widens the same idea: every docker-tier
refusal lives in `validate_network`, every shell-out behind it is a named
module-level function that can be replaced without a daemon, and the order of
the refusals is part of what is pinned. Two of T002's tests are amended here,
both priced in the spec (D10, D11): the docker-without-network case it accepted
is now refused, and an accepted network is one where every probe was consulted.
"""

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from edad import session
from edad.egress import EgressError, proxy_name, proxy_url
from edad.session import (
    Abort,
    SessionLog,
    agent_argv,
    build_parser,
    cmd_run,
    deny_enforcement,
    network_rule,
    permit_probe_argv,
    validate_network,
)

IMAGE = "edad-agent:latest"
WORKDIR = Path("/work/T002")
NAME = "edad-fixtures"
TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN = "sk-ant-oat01-not-a-real-token"


def flag_value(argv, flag):
    return argv[argv.index(flag) + 1]


def patch_probe(monkeypatch, answer, calls=None):
    """Stand in for `docker network inspect`. `answer` is what the probe
    returns: the stripped '{{.Internal}}' output, or None for every way docker
    can decline to answer — no such network, no daemon, no docker."""
    def fake(name):
        if calls is not None:
            calls.append(name)
        return answer

    monkeypatch.setattr(session, "docker_network_internal", fake)


class Probes:
    """Every shell-out `validate_network` makes, replaced at the module and
    recorded in order. The defaults describe a network that passes: internal,
    no egress, a proxy that refuses the wrong host and permits the model API,
    and a token in the environment. Each test turns one answer wrong."""

    def __init__(  # noqa: PLR0913  # one switch per probe; that is the point
        self, monkeypatch, *, internal="true", egress=False, refuses=True,
        permits=(0, "ok"), token=TOKEN,
    ):
        self.calls: list[tuple] = []

        def internal_probe(name):
            self.calls.append(("internal", name))
            return internal

        def egress_probe(network, image):
            self.calls.append(("egress", network, image))
            return egress

        def refuses_probe(network, image):
            self.calls.append(("refuses", network, image))
            return refuses

        def permits_probe(network, image):
            self.calls.append(("permits", network, image))
            return permits

        monkeypatch.setattr(session, "docker_network_internal", internal_probe)
        monkeypatch.setattr(session, "docker_network_egress", egress_probe)
        monkeypatch.setattr(session, "proxy_refuses", refuses_probe)
        monkeypatch.setattr(session, "proxy_permits", permits_probe)
        if token is None:
            monkeypatch.delenv(TOKEN_VAR, raising=False)
        else:
            monkeypatch.setenv(TOKEN_VAR, token)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


# --- the container's argv ---------------------------------------------------


def test_the_default_docker_run_still_has_no_network():
    argv = agent_argv("do the work", WORKDIR, "docker", IMAGE, yolo=False)

    assert argv[:3] == ["docker", "run", "--rm"]
    assert flag_value(argv, "--network") == "none"


def test_a_named_network_replaces_the_none_isolation():
    argv = agent_argv("do the work", WORKDIR, "docker", IMAGE, yolo=False, network=NAME)

    assert flag_value(argv, "--network") == NAME
    # One tier per run. A second --network is not a stricter run: docker takes
    # the flag more than once, and the surviving attachment would be whichever
    # ordering happened to win.
    assert argv.count("--network") == 1
    assert "none" not in argv


def test_an_unsandboxed_run_never_becomes_a_docker_run():
    """validate_network refuses this combination before argv is built, so this
    is the second line: a name arriving anyway must not conjure a container
    around a run the operator asked to leave unsandboxed."""
    argv = agent_argv("do the work", WORKDIR, "none", IMAGE, yolo=False, network=NAME)

    assert argv[0] != "docker"
    assert "--network" not in argv


# --- the refusals -----------------------------------------------------------


def test_a_network_named_without_the_docker_sandbox_is_refused():
    with pytest.raises(Abort) as excinfo:
        validate_network("none", NAME)

    assert "--sandbox docker" in str(excinfo.value)


def test_a_network_that_exists_but_is_not_internal_is_refused(monkeypatch):
    """The whole reason the probe exists. A network called anything at all can
    have a gateway, and attaching to it would put an unenforceable 'no internet'
    into every prompt of the session."""
    patch_probe(monkeypatch, "false")

    with pytest.raises(Abort) as excinfo:
        validate_network("docker", NAME)

    message = str(excinfo.value)
    assert NAME in message
    assert "internal" in message


def test_a_network_docker_cannot_report_on_is_refused(monkeypatch):
    """Missing network, stopped daemon and absent docker are one answer here:
    the isolation was not measured, so it must not be promised."""
    patch_probe(monkeypatch, None)

    with pytest.raises(Abort) as excinfo:
        validate_network("docker", NAME)

    assert NAME in str(excinfo.value)


def test_an_internal_network_is_accepted(monkeypatch):
    """Amended for D11 and D16: accepted means every probe was consulted, once,
    about the network the operator named. Stubbing only the internal probe
    would send the others to a real docker under the test."""
    probes = Probes(monkeypatch)

    skipped = validate_network("docker", NAME)

    assert skipped == []
    assert probes.calls == [
        ("internal", NAME),
        ("egress", NAME, IMAGE),
        ("refuses", NAME, IMAGE),
        ("permits", NAME, IMAGE),
    ]


def test_the_default_tier_asks_docker_nothing(monkeypatch):
    """A run with no --network makes no claim that needs checking, and probing
    anyway would make an unrelated docker problem refuse sessions that work.

    Amended for D10: `validate_network("docker", None)` used to be accepted
    here. That combination runs `--network none`, where the CLI cannot reach
    the model, so it is refused now - and refused before any probe is asked,
    which is the half of the old assertion that still holds."""
    probes = Probes(monkeypatch)

    validate_network("none", None)
    with pytest.raises(Abort):
        validate_network("docker", None)

    assert probes.calls == []


# --- what the agent is told -------------------------------------------------


def test_the_rule_for_a_plain_docker_run_is_unchanged():
    text = network_rule("docker")

    assert "--network none" in text
    assert "installs" in text


def test_the_rule_for_an_unsandboxed_run_is_unchanged():
    text = network_rule("none")

    assert "not mechanically enforced" in text
    assert "--network none" not in text


def test_the_rule_for_the_named_tier_describes_what_is_actually_reachable():
    text = network_rule("docker", network=NAME)

    assert NAME in text
    assert "container name" in text          # how the agent addresses the services
    assert "internet" in text                # and what it still cannot reach
    assert "installs" in text
    # The container is on a network now. Repeating the old sentence would be a
    # lie of exactly the kind network_rule's docstring exists to prevent.
    assert "--network none" not in text


def test_the_named_tier_honours_the_continuation_indent():
    """The rule is interpolated as item 4 of a numbered list in initial_prompt;
    a branch that forgets the indent breaks the list rather than the meaning,
    which is the sort of thing nobody notices until an agent misreads it."""
    text = network_rule("docker", cont_indent="   ", network=NAME)
    lines = text.split("\n")

    assert len(lines) > 1
    assert all(line.startswith("   ") for line in lines[1:])


# --- the CLI wiring ---------------------------------------------------------


def test_the_run_parser_defaults_to_no_named_network():
    args = build_parser().parse_args(["run", "T001"])

    assert args.network is None
    assert args.sandbox == "none"


def test_the_run_parser_takes_a_network_name():
    args = build_parser().parse_args(
        ["run", "T001", "--sandbox", "docker", "--network", NAME]
    )

    assert args.network == NAME


def test_the_network_flag_requires_a_name():
    """--network is the name of a network, never a bare on-switch: an optional
    value would give the flag a default the operator never chose."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "T001", "--network"])


# =============================================================================
# T016 — preflight measures the run the agent will get
# =============================================================================


# --- D10: bare --sandbox docker ------------------------------------------------


def test_docker_without_a_network_is_refused_at_preflight(monkeypatch):
    """Without a network the container runs --network none, where the CLI
    cannot reach the model at all: not a stricter tier, a broken one, and the
    symptom is a 15-retry storm at AGENT_TIMEOUT_S. One working docker
    configuration rather than one working and one broken."""
    probes = Probes(monkeypatch)

    with pytest.raises(Abort) as excinfo:
        validate_network("docker", None)

    message = str(excinfo.value)
    assert "never run" in message
    assert "--network" in message
    assert probes.calls == [], "refused before anything is asked of docker"


# --- D11: measured egress ------------------------------------------------------


def test_a_network_with_measured_egress_is_refused(monkeypatch):
    """`Internal: true` no longer backs the claim on its own: a forwarding
    process on the network is egress whatever docker calls the network. So an
    unconfigured throwaway container tries to reach out, and reaching out is
    a refusal about egress, not about the token or the proxy."""
    probes = Probes(monkeypatch, egress=True, token=None)

    with pytest.raises(Abort) as excinfo:
        validate_network("docker", NAME)

    message = str(excinfo.value)
    assert "egress" in message
    assert NAME in message
    assert probes.names() == ["internal", "egress"], "refused before the token is consulted"


def test_an_egress_probe_that_could_not_run_is_refused(monkeypatch):
    """The same answer as the internal probe's None: not measured is not
    promised, whatever the reason the measurement did not happen."""
    probes = Probes(monkeypatch, egress=None, token=None)

    with pytest.raises(Abort) as excinfo:
        validate_network("docker", NAME)

    assert "egress" in str(excinfo.value)
    assert probes.names() == ["internal", "egress"]


# --- D18: the token ------------------------------------------------------------


def test_docker_without_an_oauth_token_is_refused_naming_setup_token(monkeypatch):
    """`docker run -e VAR` with VAR unset passes nothing and raises no error,
    so an operator who never minted a token gets a container with no
    credential and no warning. 'Unset' deserves a better message than a
    failed API call: the variable, and the command that mints it. Sits after
    the isolation check and before the proxy probes, which need it."""
    probes = Probes(monkeypatch, token=None)

    with pytest.raises(Abort) as excinfo:
        validate_network("docker", NAME)

    message = str(excinfo.value)
    assert "claude setup-token" in message
    assert TOKEN_VAR in message
    assert probes.names() == ["internal", "egress"], "the proxy is not probed without a token"


def test_the_token_value_never_appears_in_argv(monkeypatch):
    """The token crosses as an environment pass-through: the bare `-e NAME`
    form, and the subprocess inherits the environment. With the variable set,
    its value is in no element of either argv - not the agent's, not the
    permit probe's - so it reaches neither a process listing nor a log."""
    monkeypatch.setenv(TOKEN_VAR, TOKEN)

    agent = agent_argv("do the work", WORKDIR, "docker", IMAGE, yolo=False, network=NAME)
    probe = permit_probe_argv(NAME, IMAGE)

    for argv in (agent, probe):
        assert TOKEN_VAR in argv
        assert argv[argv.index(TOKEN_VAR) - 1] == "-e", "the bare -e NAME form"
        assert not any(TOKEN in element for element in argv), argv


# --- D16: the proxy, both directions ------------------------------------------


def test_a_proxy_that_permits_a_non_allowlisted_host_is_refused(monkeypatch):
    """A CONNECT to a host outside the allowlist must come back as anything
    but 200. A proxy that permits it is a hole in the boundary; one that could
    not be probed is a boundary nobody measured. The permit probe spends a
    model turn, so it is not reached when this half already refused."""
    permissive = Probes(monkeypatch, refuses=False)
    with pytest.raises(Abort) as excinfo:
        validate_network("docker", NAME)
    assert "proxy" in str(excinfo.value)
    assert permissive.names() == ["internal", "egress", "refuses"]

    unreachable = Probes(monkeypatch, refuses=None)
    with pytest.raises(Abort) as excinfo:
        validate_network("docker", NAME)
    assert "proxy" in str(excinfo.value)
    assert unreachable.names() == ["internal", "egress", "refuses"]


def test_a_proxy_that_refuses_the_model_api_is_refused(monkeypatch):
    """The other direction, because a dead or block-everything proxy passes a
    refusal-only check and surfaces as the retry storm at 3am. The probe is
    `claude -p` itself, and its output is in the message: a dead proxy (403,
    20s of retries) and a bad token (401, 3s) print the same
    `Failed to authenticate` prefix and are told apart by the status."""
    tail = "Failed to authenticate: 403 Forbidden from proxy"
    dead = Probes(monkeypatch, permits=(1, tail))
    with pytest.raises(Abort) as excinfo:
        validate_network("docker", NAME)
    message = str(excinfo.value)
    assert "proxy" in message
    assert tail in message, "the operator needs the status to tell 403 from 401"
    assert dead.names() == ["internal", "egress", "refuses", "permits"]

    not_run = Probes(monkeypatch, permits=None)
    with pytest.raises(Abort):
        validate_network("docker", NAME)
    assert not_run.names() == ["internal", "egress", "refuses", "permits"]


def test_the_permit_probe_uses_the_token_and_image_the_agent_will_hold(monkeypatch):
    """One call proves three things - the proxy permits the API, the token is
    valid, and it is the token the agent will hold - only if the probe is the
    agent's own client, in the agent's own image, on the named network, with
    the token crossing the way it will cross for the agent."""
    monkeypatch.setenv(TOKEN_VAR, TOKEN)
    argv = permit_probe_argv(NAME, IMAGE)

    assert argv[:3] == ["docker", "run", "--rm"]
    assert flag_value(argv, "--network") == NAME
    assert argv.count("--network") == 1
    assert IMAGE in argv
    assert argv[argv.index(TOKEN_VAR) - 1] == "-e"
    assert f"HTTPS_PROXY={proxy_url(NAME)}" in argv
    assert f"HTTP_PROXY={proxy_url(NAME)}" in argv
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in argv
    tail = argv[argv.index(IMAGE) + 1:]
    assert tail[:2] == ["claude", "-p"]
    assert flag_value(tail, "--max-turns") == "1"

    # And validate_network hands the probe the image the session was given,
    # not the default: a --image override is exactly the case where the two
    # could differ.
    probes = Probes(monkeypatch)
    validate_network("docker", NAME, image="edad-agent:candidate")
    assert ("permits", NAME, "edad-agent:candidate") in probes.calls
    assert ("refuses", NAME, "edad-agent:candidate") in probes.calls
    assert ("egress", NAME, "edad-agent:candidate") in probes.calls


# --- D9, D15: what the container is handed, and what the agent is told ---------


def test_the_agent_container_is_pointed_at_the_proxy():
    """The agent stays on the internal net with no egress of its own and
    reaches the model only through the proxy, by the deterministic name that
    resolves on that net. Both variables, because the CLI honours HTTPS_PROXY
    and a stray plain-HTTP call would otherwise try to leave directly."""
    argv = agent_argv("do the work", WORKDIR, "docker", IMAGE, yolo=False, network=NAME)

    url = proxy_url(NAME)
    assert proxy_name(NAME) in url
    assert f"HTTPS_PROXY={url}" in argv
    assert f"HTTP_PROXY={url}" in argv
    assert argv[argv.index(f"HTTPS_PROXY={url}") - 1] == "-e"
    assert argv[argv.index(f"HTTP_PROXY={url}") - 1] == "-e"
    assert flag_value(argv, "--network") == NAME

    # No network, no proxy to point at. D10 refuses this combination a step
    # earlier; the builder stays pure and stays what T002 pinned.
    plain = agent_argv("do the work", WORKDIR, "docker", IMAGE, yolo=False)
    assert not any(element.startswith(("HTTPS_PROXY=", "HTTP_PROXY=")) for element in plain)


def test_nonessential_traffic_is_disabled_in_the_container():
    """Measured to drop the telemetry host entirely, which is what makes the
    allowlist one entry rather than two: the egress claim is 'the model API,
    nothing else' only because nothing else is attempted."""
    argv = agent_argv("do the work", WORKDIR, "docker", IMAGE, yolo=False, network=NAME)

    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in argv
    assert argv[argv.index("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1") - 1] == "-e"
    # Set for the container, not exported to the host: an environment variable
    # in the harness's own process would silence telemetry for the operator's
    # interactive CLI too, which is not what was decided.
    assert argv.index("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1") < argv.index(IMAGE)


def test_the_named_tier_rule_names_the_proxy_as_the_only_egress():
    """'No internet egress' was true under T002 and is now a lie by omission:
    there is one route out, and an agent told there is none will not
    understand why the model answers. The sentence says what preflight has
    just measured - the model API, via the proxy, and nothing else."""
    text = network_rule("docker", network=NAME)

    assert "model API" in text
    assert "proxy" in text
    assert NAME in text
    assert "container name" in text
    assert "internet" in text
    assert "installs" in text
    assert "--network none" not in text

    # The other two tiers have no proxy, and must not claim one.
    assert "proxy" not in network_rule("docker")
    assert "proxy" not in network_rule("none")


# --- D17: the session half -----------------------------------------------------


class Session:
    """Drives `cmd_run` up to and including preflight with every side effect
    replaced: the repo root, the ticket, preflight itself, and the proxy's
    ensure and remove. Records the order the lifecycle calls land in. The
    preflight stand-in raises `Abort` by default, because the substance of
    D17's session half is that the proxy a session created is removed on a
    refusal that happens after it was created."""

    def __init__(  # noqa: PLR0913  # one switch per stand-in
        self, monkeypatch, tmp_path, *, created=True, preflight_raises=True,
        ensure_raises=None, dry_run=False, skipped=(),
    ):
        self.calls: list[tuple] = []
        self.args = SimpleNamespace(
            ticket="T999", sandbox="docker", network=NAME, image=IMAGE,
            yolo=False, dry_run=dry_run,
        )

        def ensure(network):
            self.calls.append(("ensure", network))
            if ensure_raises is not None:
                raise ensure_raises
            return created

        def remove(network):
            self.calls.append(("remove", network))

        def preflight(root, ticket, sandbox, dry_run, network=None, image=None):  # noqa: PLR0913  # mirrors the real one
            self.calls.append(("preflight", sandbox, network, image, dry_run))
            if preflight_raises:
                raise Abort("refused after the proxy was ensured")
            return list(skipped)

        def make_worktree(root, ticket_id, base):
            self.calls.append(("worktree", ticket_id))
            return tmp_path / "wt", f"edad/{ticket_id.lower()}"

        monkeypatch.setattr(session, "repo_root", lambda: tmp_path)
        monkeypatch.setattr(session, "load_ticket",
                            lambda root, tid: {"id": tid, "title": "a ticket"})
        monkeypatch.setattr(session, "git", lambda *a, **k: "0" * 40)
        monkeypatch.setattr(session, "ensure_egress_proxy", ensure)
        monkeypatch.setattr(session, "remove_egress_proxy", remove)
        monkeypatch.setattr(session, "preflight", preflight)
        monkeypatch.setattr(session, "make_worktree", make_worktree)

    def run(self) -> int | None:
        """The exit code, or None when the refusal propagated as Abort - which
        is what main() turns into exit 2. Either is a refused session; what is
        pinned is what happened to the proxy on the way out."""
        try:
            return cmd_run(self.args)
        except Abort:
            return None

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def test_a_standalone_session_creates_and_removes_its_own_proxy(monkeypatch, tmp_path):
    """Ensure before preflight, because the proxy must exist before
    validate_network can probe it; remove in a finally, because a refusal for
    any later reason - no lock, dirty tree - has already created a container
    that a teardown at the end of the session would never reach."""
    s = Session(monkeypatch, tmp_path, created=True)

    s.run()

    assert s.names() == ["ensure", "preflight", "remove"], s.calls
    assert s.calls[0] == ("ensure", NAME)
    assert s.calls[-1] == ("remove", NAME)
    preflight = s.calls[1]
    assert preflight[1:4] == ("docker", NAME, IMAGE), "preflight probes the image the agent gets"

    # A proxy that could not be ensured is the session's refusal, in the
    # session's own vocabulary - and preflight is never reached.
    failed = Session(monkeypatch, tmp_path, ensure_raises=EgressError("no image: build it"))
    with pytest.raises(Abort) as excinfo:
        cmd_run(failed.args)
    assert "no image: build it" in str(excinfo.value)
    assert failed.names() == ["ensure"]


def test_a_session_leaves_a_proxy_it_found_already_present(monkeypatch, tmp_path):
    """Whoever created it destroys it. A child of the queue finds the queue's
    proxy present and must leave it for the sessions after it; removing it
    would hand every later ticket the retry storm."""
    s = Session(monkeypatch, tmp_path, created=False)

    s.run()

    assert s.names() == ["ensure", "preflight"], s.calls
    assert not any(name == "remove" for name in s.names())


# --- D8: the recording half ----------------------------------------------------


def test_the_session_log_records_which_half_of_the_deny_was_enforced():
    """`network_access: deny` is two claims. On the agent container it is
    mechanical under docker and a sentence in a prompt otherwise; on the
    verifier it is `EDAD_NETWORK=deny`, a string a cooperating suite may
    honour, and nothing more. The log says which, per run, so nobody reads
    'deny' in a ticket and assumes both."""
    deny = {"id": "T1", "kill_conditions": {"network_access": "deny"}}
    allow = {"id": "T2", "kill_conditions": {"network_access": "allow"}}
    unstated = {"id": "T3"}

    assert deny_enforcement(deny, "docker") == {"agent": "enforced", "verifier": "advisory"}
    assert deny_enforcement(deny, "none") == {"agent": "advisory", "verifier": "advisory"}
    assert deny_enforcement(allow, "docker") == {"agent": "not requested",
                                                 "verifier": "not requested"}
    assert deny_enforcement(unstated, "docker") == deny_enforcement(allow, "docker")

    # And the log has somewhere to put it, beside the tier it was measured
    # under. Both default to "nothing", so a log from a --sandbox none run
    # still says so rather than omitting the fields.
    log = asdict(SessionLog(ticket="T1", started_at="now", base_commit="0" * 40,
                            branch="edad/t1", sandbox="docker"))
    keys = list(log)
    assert keys[keys.index("sandbox") + 1] == "network"
    assert keys[keys.index("sandbox") + 2] == "network_access"
    assert log["network"] is None
    assert log["network_access"] == {}


# --- settled here: --dry-run under the docker tier ------------------------------


def test_a_dry_run_runs_the_isolation_check_only_and_says_so(monkeypatch, tmp_path, capsys):
    """A dry-run exists to print the prompt. With the probes it would start a
    container and spend a model turn to do it, so it runs the isolation check
    only - internal, no egress - skips the token check and both proxy probes,
    starts no proxy, and says what it skipped, so the operator is not told
    the network was validated when half of the validation did not happen."""
    probes = Probes(monkeypatch, token=None)

    skipped = validate_network("docker", NAME, dry_run=True)

    assert probes.names() == ["internal", "egress"]
    assert len(skipped) == 3, skipped
    assert any("token" in name for name in skipped), skipped
    assert sum("proxy" in name for name in skipped) == 2, skipped
    # The isolation check still refuses: a dry-run on a network with egress
    # prints a prompt that claims an isolation nobody measured.
    Probes(monkeypatch, egress=True, token=None)
    with pytest.raises(Abort):
        validate_network("docker", NAME, dry_run=True)

    s = Session(monkeypatch, tmp_path, dry_run=True, preflight_raises=False,
                skipped=["oauth token", "proxy refuses", "proxy permits"])
    assert s.run() == 0
    assert "ensure" not in s.names(), "a dry-run starts no proxy"
    assert "remove" not in s.names()
    out = capsys.readouterr().out
    dry_line = next(line for line in out.splitlines() if "[dry-run]" in line)
    for name in ("oauth token", "proxy refuses", "proxy permits"):
        assert name in dry_line, out

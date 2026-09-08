"""
FROZEN ACCEPTANCE TEST — ticket T002.

Authored at the approve step, before implementation exists. The build agent may
read and run this file. It may NOT modify it. The gate runner verifies the file
hash before every run; a mismatch is a hard fail and aborts the session.

Run: pytest tests/test_session_network.py -q
Expected before implementation: collection error (the names do not exist yet).

Nothing here talks to docker. The point of the tier is a promise the harness has
measured rather than repeated, so what needs proving is the measuring, the
refusing and the sentence the agent is told — and a test that needed a running
daemon would be skipped on precisely the machines where a wrong answer is
cheapest to ship.
"""

from pathlib import Path

import pytest

from edad import session
from edad.session import (
    Abort,
    agent_argv,
    build_parser,
    network_rule,
    validate_network,
)

IMAGE = "edad-agent:latest"
WORKDIR = Path("/work/T002")
NAME = "edad-fixtures"


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
    calls = []
    patch_probe(monkeypatch, "true", calls)

    validate_network("docker", NAME)

    assert calls == [NAME], "the network the operator named is the one inspected"


def test_the_default_tier_asks_docker_nothing(monkeypatch):
    """A run with no --network makes no claim that needs checking, and probing
    anyway would make an unrelated docker problem refuse sessions that work."""
    calls = []
    patch_probe(monkeypatch, "true", calls)

    validate_network("docker", None)
    validate_network("none", None)

    assert calls == []


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

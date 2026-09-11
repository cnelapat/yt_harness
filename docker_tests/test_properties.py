"""
CONTAINER PROPERTY TEST — the docker tier's proof of being a sandbox (D2, D7, D14).

Everything the tickets verified is a string builder plus a stubbed shell-out;
this is the one place those claims meet a real daemon. Hand-written and
hand-run, per D12: its deliverable is a test file, so there is no ticket.

Opt-in, not auto-skip. It lives outside `pyproject.toml`'s `testpaths` so no
`full_gate` collects it, and the module refuses to import without
EDAD_DOCKER_TESTS=1 so that running it by path without the variable is an
error rather than a green nothing:

    EDAD_DOCKER_TESTS=1 python3 -m pytest docker_tests/test_properties.py -q

Needs: a daemon, `python:3.12-slim`, and the built `edad-egress:latest`. No API
key: the proxy test proves CONNECT semantics against the real image, which
takes one TCP handshake to api.anthropic.com and nothing after it. Every
network and container this file creates carries a per-run suffix and is
removed at teardown, whatever the outcome.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid

import pytest

if os.environ.get("EDAD_DOCKER_TESTS") != "1":
    raise RuntimeError("docker property tests are opt-in: set EDAD_DOCKER_TESTS=1")

from edad.egress import ensure_egress_proxy, proxy_name, remove_egress_proxy
from edad.egress_proxy import ALLOWLIST, PORT

IMAGE = "python:3.12-slim"
FIXTURE_PORT = 8000
OUTSIDE = ("1.1.1.1", 443)  # egress is probed by IP so a DNS failure cannot masquerade as a deny

# Runs inside a throwaway container; prints one JSON object. `reach` retries so a
# fixture still booting reads as reachable, and gives up fast so a deny reads as a
# deny in seconds. `connect` returns the proxy's status line for one CONNECT.
PROBE = r"""
import json, socket, sys, time
def reach(host, port, tries):
    for _ in range(tries):
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            time.sleep(1)
    return False
def connect(proxy, target):
    with socket.create_connection((proxy, PROXY_PORT), timeout=10) as s:
        s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        return s.recv(1024).split(b"\r\n")[0].decode()
spec = json.loads(sys.argv[1])
out = {"outside": reach(OUTSIDE_HOST, OUTSIDE_PORT, 2)}
if "fixture" in spec:
    out["fixture"] = reach(spec["fixture"], FIXTURE_PORT, 15)
if "proxy" in spec:
    out["connect"] = {t: connect(spec["proxy"], t) for t in spec["targets"]}
print(json.dumps(out))
"""
PROBE = (f"PROXY_PORT={PORT}; OUTSIDE_HOST={OUTSIDE[0]!r}; OUTSIDE_PORT={OUTSIDE[1]}; "
         f"FIXTURE_PORT={FIXTURE_PORT}\n") + PROBE


def docker(*args: str) -> str:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120,
                          check=False)
    assert proc.returncode == 0, f"docker {' '.join(args)}\n{proc.stderr}"
    return proc.stdout.strip()


def probe(network: str, **spec: object) -> dict:
    out = docker("run", "--rm", "--network", network, IMAGE, "python3", "-c", PROBE,
                 json.dumps(spec))
    return json.loads(out)


def host_reaches(port: int, tries: int = 15) -> bool:
    for _ in range(tries):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3):
                return True
        except OSError:
            time.sleep(1)
    return False


class Sandbox:
    """One internal network per test, and every container started through it
    is removed with it."""

    def __init__(self) -> None:
        self.tag = uuid.uuid4().hex[:8]
        self.network = f"edad-prop-{self.tag}"
        self.containers: list[str] = []
        docker("network", "create", "--internal", self.network)

    def fixture(self, *run_flags: str) -> str:
        name = f"edad-fixture-{self.tag}"
        docker("run", "-d", "--rm", "--name", name, *run_flags, IMAGE,
               "python3", "-m", "http.server", str(FIXTURE_PORT), "--bind", "0.0.0.0")
        self.containers.append(name)
        return name

    def close(self) -> None:
        for name in self.containers:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", self.network], capture_output=True, check=False)


@pytest.fixture
def sandbox():
    box = Sandbox()
    try:
        yield box
    finally:
        box.close()


def test_a_container_on_network_none_has_no_egress():
    assert probe("none") == {"outside": False}


def test_a_container_on_the_internal_net_reaches_the_fixture_by_name_and_has_no_egress(sandbox):
    fixture = sandbox.fixture("--network", sandbox.network)
    assert probe(sandbox.network, fixture=fixture) == {"outside": False, "fixture": True}


def test_a_fixture_published_bridge_first_is_reachable_from_the_host_and_by_name(sandbox):
    # D2's recipe, verbatim: start on bridge with -p, then connect to the internal net.
    fixture = sandbox.fixture("--network", "bridge", "-p", f"127.0.0.1::{FIXTURE_PORT}")
    docker("network", "connect", sandbox.network, fixture)
    port = int(docker("port", fixture, str(FIXTURE_PORT)).rsplit(":", 1)[1])
    assert host_reaches(port), "the verifier, on the host, must reach the published port"
    assert probe(sandbox.network, fixture=fixture)["fixture"] is True


def test_a_fixture_on_the_internal_net_alone_cannot_publish_a_host_port(sandbox):
    # Docker accepts the run and publishes nothing: no mapping, no host port. This
    # silence is why the recipe is bridge-first — the wrong order looks like success.
    fixture = sandbox.fixture("--network", sandbox.network, "-p", f"127.0.0.1::{FIXTURE_PORT}")
    assert docker("port", fixture) == ""
    assert json.loads(docker("inspect", "-f", "{{json .NetworkSettings.Ports}}", fixture)) == {}


def test_the_real_proxy_image_permits_only_the_model_api(sandbox):
    # The lifecycle code itself starts it: bridge first, then attached, as a night would.
    assert ensure_egress_proxy(sandbox.network) is True
    try:
        (allowed,) = ALLOWLIST
        out = probe(sandbox.network, proxy=proxy_name(sandbox.network),
                    targets=[f"{allowed}:443", "example.com:443", "1.1.1.1:443"])
    finally:
        remove_egress_proxy(sandbox.network)
    assert out["outside"] is False, "the proxy must be the only way out"
    assert out["connect"][f"{allowed}:443"] == "HTTP/1.1 200 Connection Established"
    assert out["connect"]["example.com:443"] == "HTTP/1.1 403 Forbidden"
    assert out["connect"]["1.1.1.1:443"] == "HTTP/1.1 403 Forbidden"

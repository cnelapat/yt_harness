"""
FROZEN ACCEPTANCE TEST — ticket T015.

The egress proxy: the CONNECT-allowlist daemon (`edad/egress_proxy.py`), the
lifecycle that starts and removes it (`edad/egress.py`), and the image the
harness builds it into (`Dockerfile.egress`).

Nothing here talks to docker or to the internet. The daemon is exercised
in-process over socket pairs with its upstream dial replaced, which is what
proves the allowlist is matched *before* any connection is attempted — the one
claim the tier's security rests on, and one a test against a live proxy can
only infer from timing. The lifecycle is exercised against a recorded stand-in
for the docker shell-out, the pattern `docker_network_internal` established.
The live half — the real image, dual-homed, permitting one host and refusing
another — is `docker_tests/test_properties.py`, opt-in and hand-run (D7, D12).

Every socket the test holds carries a timeout. A daemon that never answers must
fail here in seconds, not hang the gate for `command_timeout_s`.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from edad import egress
from edad.egress import (
    BUILD_COMMAND,
    IMAGE,
    EgressError,
    ensure_egress_proxy,
    proxy_name,
    proxy_url,
    remove_egress_proxy,
)
from edad.egress_proxy import ALLOWLIST, PORT, handle

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NETWORK = "edad-fixtures"
MODEL_API = "api.anthropic.com"
TIMEOUT_S = 2.0

CONNECT_MODEL_API = b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\r\n\r\n"
CONNECT_OTHER_HOST = b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
CONNECT_RAW_IP = b"CONNECT 1.1.1.1:443 HTTP/1.1\r\nHost: 1.1.1.1:443\r\n\r\n"
GET_MODEL_API = b"GET http://api.anthropic.com/ HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n"


def read_head(sock: socket.socket) -> bytes:
    """Everything up to the end of the response head, or up to EOF."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


class Wire:
    """One connection through the daemon, with no network anywhere.

    `client` is the agent's end. `handle` runs in a thread against the other
    end of the pair, with `dial` replaced by a stand-in that records the target
    it was asked for and hands back one end of a second pair; `upstream` is the
    far end of that pair, the test playing the model API. A refused request
    must never reach the stand-in at all: `dials` stays empty.
    """

    def __init__(self, **kw):
        self.client, proxy_end = socket.socketpair()
        self.client.settimeout(TIMEOUT_S)
        self.dials: list[tuple[str, int]] = []
        self.upstream: socket.socket | None = None

        def dial(host: str, port: int) -> socket.socket:
            self.dials.append((host, port))
            near, far = socket.socketpair()
            far.settimeout(TIMEOUT_S)
            self.upstream = far
            return near

        self.thread = threading.Thread(
            target=handle, args=(proxy_end,), kwargs={"dial": dial, **kw}, daemon=True
        )
        self.thread.start()

    def request(self, head: bytes) -> bytes:
        self.client.sendall(head)
        return read_head(self.client)

    def close(self) -> None:
        self.client.close()
        if self.upstream is not None:
            self.upstream.close()
        self.thread.join(TIMEOUT_S)


@pytest.fixture
def wire():
    w = Wire()
    yield w
    w.close()


# --- D14: the daemon ---------------------------------------------------------


def test_connect_to_the_allowlisted_host_is_spliced_to_the_upstream(wire):
    """The whole job: a 200, then bytes pass both ways untouched. What is
    spliced is opaque - the TLS handshake starts after the 200, so the proxy
    never sees a plaintext byte of the conversation it is carrying."""
    status = wire.request(CONNECT_MODEL_API)

    assert status.startswith(b"HTTP/1.1 200"), status
    assert wire.dials == [(MODEL_API, 443)]

    wire.client.sendall(b"\x16\x03\x01 client hello")
    assert wire.upstream.recv(4096) == b"\x16\x03\x01 client hello"
    wire.upstream.sendall(b"\x16\x03\x03 server hello")
    assert wire.client.recv(4096) == b"\x16\x03\x03 server hello"

    # And the client hanging up reaches the upstream as EOF rather than leaving
    # a half-open connection behind on the model's side.
    wire.client.close()
    assert wire.upstream.recv(4096) == b""


def test_connect_to_any_other_host_is_refused_before_any_upstream_connection(wire):
    """Refused, and refused *first*: the target host is in the CONNECT line
    before any TLS, so the decision needs no connection to be attempted. A
    proxy that dialled and then refused would already have made the egress
    the allowlist exists to prevent."""
    status = wire.request(CONNECT_OTHER_HOST)

    assert status.startswith(b"HTTP/1.1 403"), status
    assert wire.dials == [], "the refusal must precede any dial"
    assert wire.client.recv(4096) == b"", "a refused connection is closed, not left open"


def test_a_raw_ip_target_is_refused(wire):
    """A DNS allowlist was rejected because DNS does not bind the connection
    and a raw IP walks past it. This one binds the CONNECT target, so the IP
    of the model API is as refused as any other string that is not its name."""
    status = wire.request(CONNECT_RAW_IP)

    assert status.startswith(b"HTTP/1.1 403"), status
    assert wire.dials == []


def test_a_non_connect_request_is_refused(wire):
    """Only tunnels. A plain proxied GET - even to the allowlisted host - is a
    request the proxy would have to read and forward itself, which is a second
    mechanism with its own surface. It has one."""
    status = wire.request(GET_MODEL_API)

    assert status.startswith(b"HTTP/1.1 4"), status
    assert wire.dials == []


# --- D15: the allowlist is one entry -----------------------------------------


def test_the_allowlist_is_exactly_the_model_api():
    """One host, by name, immutable. Telemetry is switched off in the container
    (T016) so that this is the whole list; a second entry here is the widening
    the auditor is meant to be able to rule out by reading one line."""
    assert ALLOWLIST == frozenset({MODEL_API})
    assert isinstance(ALLOWLIST, frozenset)

    # And it is the parameter that decides, so the default above is what the
    # daemon matches against rather than a constant nothing reads.
    other = Wire(allowlist=frozenset({"example.com"}))
    try:
        assert other.request(CONNECT_OTHER_HOST).startswith(b"HTTP/1.1 200")
        assert other.dials == [("example.com", 443)]
    finally:
        other.close()
    refused = Wire(allowlist=frozenset({"example.com"}))
    try:
        assert refused.request(CONNECT_MODEL_API).startswith(b"HTTP/1.1 403")
        assert refused.dials == []
    finally:
        refused.close()


# --- D13: the harness owns the image ------------------------------------------


def test_the_proxy_image_is_built_from_the_checked_in_daemon():
    """The boundary is one file, and the image is that file and an interpreter.
    Nothing is fetched at build time: a `pip install` or an `apt-get` in this
    Dockerfile would make the security boundary depend on what a registry
    served on the day it was built."""
    dockerfile = (PROJECT_ROOT / "Dockerfile.egress").read_text()
    lines = [line.strip() for line in dockerfile.splitlines() if line.strip()]

    copies = [line for line in lines if line.startswith("COPY")]
    assert any("edad/egress_proxy.py" in line for line in copies), copies
    entry = [line for line in lines if line.startswith(("CMD", "ENTRYPOINT"))]
    assert any("egress_proxy" in line for line in entry), entry
    for forbidden in ("pip install", "apt-get", "curl", "wget", "npm"):
        assert not any(forbidden in line for line in lines), forbidden

    assert IMAGE == "edad-egress:latest"
    assert "Dockerfile.egress" in BUILD_COMMAND
    assert IMAGE in BUILD_COMMAND


# --- D17: the lifecycle, and D13's dual-homing --------------------------------


class Docker:
    """Stands in for `egress.docker`, the one shell-out: records every argv it
    was handed and answers from two switches. `(returncode, stdout)`, or None
    for every way docker can fail to answer at all."""

    def __init__(self, present: bool = False, image: bool = True):
        self.present = present
        self.image = image
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str] | None:
        self.calls.append(list(args))
        head = tuple(args[:2])
        if head == ("container", "inspect"):
            return (0, "true") if self.present else (1, "")
        if head == ("image", "inspect"):
            return (0, "sha256:0") if self.image else (1, "")
        if args[0] == "run":
            return (0, "0123456789ab")
        return (0, "")

    def runs(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "run"]


def patch_docker(monkeypatch, **kw) -> Docker:
    fake = Docker(**kw)
    monkeypatch.setattr(egress, "docker", fake)
    return fake


def test_the_proxy_name_is_deterministic_per_network():
    """`edad-egress-<network>`: the queue and every child session it spawns
    compute the same name from the same flag, which is how a child finds the
    proxy the queue started without being told about it."""
    assert proxy_name(NETWORK) == "edad-egress-edad-fixtures"
    assert proxy_name(NETWORK) == proxy_name(NETWORK)
    assert proxy_name("other") != proxy_name(NETWORK)

    # What the agent container is pointed at: the name resolves on the internal
    # network, and the port is the daemon's own constant, imported not copied.
    assert isinstance(PORT, int)
    assert proxy_url(NETWORK) == f"http://{proxy_name(NETWORK)}:{PORT}"


def test_ensure_starts_the_proxy_on_bridge_first_then_attaches_the_internal_net(monkeypatch):
    """The one ordering docker supports (D2, measured): a container started on
    an internal network can never reach out, so the proxy starts on bridge and
    is then connected to the internal net. Started the other way round it is
    a proxy with nothing behind it - and the agent sees the 15-retry storm."""
    fake = patch_docker(monkeypatch)

    ensure_egress_proxy(NETWORK)

    runs = fake.runs()
    assert len(runs) == 1, fake.calls
    run = runs[0]
    name = proxy_name(NETWORK)
    assert run[run.index("--name") + 1] == name
    assert run[run.index("--network") + 1] == "bridge"
    assert run.count("--network") == 1
    assert "-d" in run, "detached; a foreground proxy blocks the session that started it"
    assert IMAGE in run
    assert NETWORK not in run, "the internal net is attached afterwards, not at start"

    connects = [c for c in fake.calls if c[:2] == ["network", "connect"]]
    assert connects == [["network", "connect", NETWORK, name]]
    assert fake.calls.index(run) < fake.calls.index(connects[0])


def test_ensure_creates_only_when_absent_and_reports_whether_it_did(monkeypatch):
    """Whoever created it destroys it (D17), which needs `ensure` to say
    whether it did. A queue ensures one and each child finds it present; a
    standalone session finds none and makes its own."""
    found = patch_docker(monkeypatch, present=True)
    assert ensure_egress_proxy(NETWORK) is False
    assert found.runs() == [], "present means left alone, not restarted"
    assert not any(c[:2] == ["network", "connect"] for c in found.calls)

    absent = patch_docker(monkeypatch, present=False)
    assert ensure_egress_proxy(NETWORK) is True
    assert len(absent.runs()) == 1


def test_ensure_refuses_when_the_image_is_absent_naming_the_build_command(monkeypatch):
    """Refused, not built. The harness does not build the agent image either;
    and "not present" deserves the build command, not the failure `docker run`
    would produce a step later."""
    fake = patch_docker(monkeypatch, image=False)

    with pytest.raises(EgressError) as excinfo:
        ensure_egress_proxy(NETWORK)

    assert BUILD_COMMAND in str(excinfo.value)
    assert fake.runs() == []


def test_remove_tears_down_by_name(monkeypatch):
    """Teardown needs nothing but the network: the name is derived, so a
    creator that crashed between ensure and remove can still be cleaned up by
    a later invocation with the same flag."""
    fake = patch_docker(monkeypatch)

    remove_egress_proxy(NETWORK)

    assert len(fake.calls) == 1, fake.calls
    call = fake.calls[0]
    assert "rm" in call
    assert "-f" in call
    assert call[-1] == proxy_name(NETWORK)
    assert fake.runs() == []

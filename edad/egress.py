"""The egress proxy's lifecycle: name, ensure, remove.

The docker plumbing around `edad.egress_proxy`, kept apart from it so the
daemon can be audited without this beside it. Both entry points import from
here: the queue ensures one proxy per night, a standalone session its own.
Whoever created it removes it, which is why `ensure_egress_proxy` reports
whether it did.

Not concurrency-safe: two standalone sessions on one network can both see
"absent" and both `run`; the second `--name` collision surfaces as an
EgressError rather than a shared proxy.
"""

from __future__ import annotations

import subprocess

from edad.egress_proxy import PORT  # the port is the daemon's; imported, not copied

IMAGE = "edad-egress:latest"
BUILD_COMMAND = f"docker build -f Dockerfile.egress -t {IMAGE} ."


class EgressError(Exception):
    """The proxy could not be ensured. Callers translate it to their own refusal."""


def docker(args: list[str]) -> tuple[int, str] | None:
    """`(returncode, stripped stdout)` of `docker <args>`, or None when docker
    could not be run at all. The only thing in this module that shells out,
    and a module-level name so the lifecycle can be tested without a daemon."""
    try:
        proc = subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=False, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.returncode, proc.stdout.strip()


def proxy_name(network: str) -> str:
    """Deterministic per network: a queue and every child it spawns compute the
    same name from the same flag."""
    return f"edad-egress-{network}"


def proxy_url(network: str) -> str:
    """What HTTPS_PROXY in the agent container is set to; the name resolves on
    the internal network."""
    return f"http://{proxy_name(network)}:{PORT}"


def ensure_egress_proxy(network: str) -> bool:
    """Start the proxy for `network` unless one is present. True if this call
    created it, False if it was already there.

    Started on bridge and *then* connected to the internal net - the one
    ordering docker supports, since a container started on an internal
    network can never reach out. Present means present, not healthy: a dead
    proxy is T016's permit probe to notice, and `--rm` means a crashed daemon
    leaves no stopped shell behind for the next ensure to find.
    """
    name = proxy_name(network)
    present = docker(["container", "inspect", name])
    if present is None:
        raise EgressError("docker could not be run; is the daemon up and on PATH?")
    if present[0] == 0:
        return False
    image = docker(["image", "inspect", IMAGE])
    if image is None or image[0] != 0:
        raise EgressError(
            f"egress proxy image {IMAGE} is not present. Build it with: {BUILD_COMMAND}"
        )
    run = docker(["run", "-d", "--rm", "--name", name, "--network", "bridge", IMAGE])
    if run is None or run[0] != 0:
        raise EgressError(f"could not start egress proxy {name}: {'' if run is None else run[1]}")
    connect = docker(["network", "connect", network, name])
    if connect is None or connect[0] != 0:
        # A proxy with no route to the agent is worse than none: a retry must not find it.
        remove_egress_proxy(network)
        raise EgressError(f"could not attach egress proxy {name} to network {network!r}")
    return True


def remove_egress_proxy(network: str) -> None:
    """Tear down by name. Removing what is not there is not an error."""
    docker(["rm", "-f", proxy_name(network)])

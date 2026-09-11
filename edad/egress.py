"""The egress proxy's lifecycle: name, ensure, remove. STUB - T015 fills it in.

The docker plumbing around `edad.egress_proxy`, kept apart from it so the
daemon can be audited without this beside it. Both entry points import from
here: the queue ensures one proxy per night, a standalone session its own.
"""

from __future__ import annotations

from edad.egress_proxy import PORT  # noqa: F401  # the port is the daemon's; imported, not copied

IMAGE = ""
BUILD_COMMAND = ""


class EgressError(Exception):
    """The proxy could not be ensured. Callers translate it to their own refusal."""


def docker(args: list[str]) -> tuple[int, str] | None:
    return None


def proxy_name(network: str) -> str:
    return ""


def proxy_url(network: str) -> str:
    return ""


def ensure_egress_proxy(network: str) -> bool:
    return False


def remove_egress_proxy(network: str) -> None:
    return None

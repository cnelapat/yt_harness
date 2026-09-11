"""The egress proxy: a CONNECT allowlist and nothing else. STUB - T015 fills it in.

What the container will run (`python3 -m edad.egress_proxy`, see
Dockerfile.egress). Stdlib only, and small enough to read in one screen,
because the allowlist below *is* the docker tier's security boundary.
"""

from __future__ import annotations

import socket
from collections.abc import Callable

ALLOWLIST: frozenset[str] = frozenset()
PORT = 0

Dial = Callable[[str, int], socket.socket]


def dial(host: str, port: int) -> socket.socket:
    return socket.create_connection((host, port))


def handle(client: socket.socket, *, allowlist: frozenset[str] = ALLOWLIST,
           dial: Dial = dial) -> None:
    with client:
        client.recv(4096)
        client.sendall(b"HTTP/1.1 500 Not Implemented\r\n\r\n")


def serve(listener: socket.socket, *, allowlist: frozenset[str] = ALLOWLIST,
          dial: Dial = dial) -> None:
    return None


def main() -> None:
    return None


if __name__ == "__main__":
    main()

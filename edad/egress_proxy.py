"""The egress proxy: a CONNECT allowlist and nothing else.

What the container runs (`python3 -m edad.egress_proxy`, see Dockerfile.egress).
Stdlib only, and small enough to read in one screen, because the allowlist
below *is* the docker tier's security boundary: a CONNECT to a host in it is
spliced to the upstream; anything else is refused before any connection is
attempted. No interception, no certificate, no decryption - the TLS handshake
starts after the 200, so the proxy never sees a plaintext byte of what it
carries.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable

ALLOWLIST: frozenset[str] = frozenset({"api.anthropic.com"})
PORT = 3128

MAX_HEAD = 64 * 1024
Dial = Callable[[str, int], socket.socket]


def dial(host: str, port: int) -> socket.socket:
    """The default upstream connection, and the only place the daemon reaches out."""
    return socket.create_connection((host, port), timeout=30)


def read_head(sock: socket.socket) -> bytes | None:
    """Everything up to the end of the request head, or None if EOF or the
    head exceeds MAX_HEAD before its blank line arrives."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk or len(buf) + len(chunk) > MAX_HEAD:
            return None
        buf += chunk
    return buf


def pump(src: socket.socket, dst: socket.socket) -> None:
    """Copy bytes src -> dst until EOF, then pass the hangup on as a write shutdown."""
    try:
        while chunk := src.recv(65536):
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client: socket.socket, *, allowlist: frozenset[str] = ALLOWLIST,
           dial: Dial = dial) -> None:
    """One connection, start to finish. The client socket is closed on every path."""
    try:
        head = read_head(client)
        if head is None:
            return
        request_line, _, rest = head.partition(b"\r\n")
        parts = request_line.split()
        if len(parts) != 3 or parts[0] != b"CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            return
        host, sep, port_text = parts[1].decode("latin-1").rpartition(":")
        if not sep or not port_text.isdigit():
            client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            return
        if host not in allowlist:
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            return
        try:
            upstream = dial(host, int(port_text))
        except OSError:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            return
        with upstream:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            # Bytes that arrived in the same read as the head belong to the tunnel.
            body = rest.partition(b"\r\n\r\n")[2]
            if body:
                upstream.sendall(body)
            back = threading.Thread(target=pump, args=(upstream, client), daemon=True)
            back.start()
            pump(client, upstream)
            back.join()
    except OSError:
        pass
    finally:
        client.close()


def serve(listener: socket.socket, *, allowlist: frozenset[str] = ALLOWLIST,
          dial: Dial = dial) -> None:
    """Accept loop: one `handle`, on its own thread, per connection."""
    while True:
        conn, _ = listener.accept()
        threading.Thread(
            target=handle, args=(conn,), kwargs={"allowlist": allowlist, "dial": dial},
            daemon=True,
        ).start()


def main() -> None:
    serve(socket.create_server(("0.0.0.0", PORT), backlog=64))


if __name__ == "__main__":
    main()

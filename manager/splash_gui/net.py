"""Small network helpers shared by the manager runner and the settings API."""

from __future__ import annotations

import errno
import os
import socket


def probe_bind(host: str, port: int) -> None:
    """Raise OSError when `host:port` cannot be served (as Splash's launcher checks)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
        address = probe.getsockname()[0]
    if address in ("0.0.0.0", "::"):  # noqa: S104
        address = "127.0.0.1" if family == socket.AF_INET else "::1"
    with socket.socket(family, socket.SOCK_STREAM) as client:
        client.settimeout(1)
        if client.connect_ex((address, port)) == 0:
            raise OSError(errno.EADDRINUSE, os.strerror(errno.EADDRINUSE))

"""`splash-gui-manager`: run the manager on `server.host:server.port` (SPEC §4.2).

Changing `server.host` or `server.port` rebinds the running manager (SPEC §8.5):
the app's lifespan runs once for the whole process, and only the uvicorn server
(the listening socket) is replaced, so the engine and SSE state survive a rebind.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import logging
import os
import signal
import socket
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .app import AppConfig, create_app
from .hardening import MAX_BODY
from .logging_setup import RedactingFilter, setup_logging
from .paths import FILE_MODE, Paths, write_atomic
from .secrets import SecretName, SecretStore, backend_from_env
from .settings import parsers as p
from .settings.store import Change, SettingsStore
from .state import ManagerState

log = logging.getLogger("splash_gui.manager")


class StartupError(RuntimeError):
    pass


@contextlib.contextmanager
def instance_lock(paths: Paths) -> Iterator[None]:
    """One manager per base directory; the pid file lives as long as the lock."""
    lock_path = paths.run_dir / "manager.lock"
    with lock_path.open("a+") as lock:
        lock_path.chmod(FILE_MODE)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pid = paths.manager_pid.read_text().strip() if paths.manager_pid.exists() else "?"
            raise StartupError(f"Splashboard manager is already running (PID {pid})") from None
        write_atomic(paths.manager_pid, f"{os.getpid()}\n".encode())
        try:
            yield
        finally:
            with contextlib.suppress(FileNotFoundError):
                paths.manager_pid.unlink()


def check_bind(
    host: str, secrets: SecretStore, require_key: bool, admin_requires_key: bool = True
) -> None:
    """SPEC §17.1: refuse a non-loopback bind without an API key, and (D42) without
    admin sign-in (a settings.json written before D42 may still hold that)."""
    if p.is_loopback_host(host):
        return
    if not require_key or not secrets.has(SecretName.API_KEY):
        raise StartupError(
            f"refusing to listen on {host} without an API key; generate one in "
            "Settings → Security or bind to 127.0.0.1"
        )
    if not admin_requires_key:
        raise StartupError(
            f"refusing to listen on {host} with admin sign-in off; turn on "
            "security.admin_requires_key (`splash config set security.admin_requires_key "
            "true`) or bind to 127.0.0.1"
        )


REBIND_DELAY_S = 0.5  # let the PUT /settings response reach the UI first
GRACEFUL_SHUTDOWN_S = 5  # SSE streams never finish on their own


class _Server(uvicorn.Server):
    """A uvicorn server that leaves the stop signals to `ManagerRunner`."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        # uvicorn would hold SIGINT/SIGTERM only while this server runs and put the old
        # handlers back when it returns, before the lifespan shutdown restores the
        # integrations; the runner keeps them for the whole run instead.
        yield


def bound_addresses(server: uvicorn.Server) -> set[tuple[str, int]]:
    """The addresses `server`'s listening sockets are bound to (getsockname)."""
    found: set[tuple[str, int]] = set()
    for listener in getattr(server, "servers", None) or ():
        for sock in getattr(listener, "sockets", None) or ():
            with contextlib.suppress(OSError):
                name = sock.getsockname()
                if isinstance(name, tuple) and len(name) >= 2:
                    found.add((str(name[0]), int(name[1])))
    return found


def listening_on(server: uvicorn.Server, host: str, port: int) -> bool:
    """Whether `server` already listens on `host:port`, compared with the bound
    sockets, not with the settings it was started from. A host name counts when every
    address it resolves to is bound on that port."""
    bound = bound_addresses(server)
    if not bound or port not in {p for _, p in bound}:
        return False
    hosts = {h for h, p in bound if p == port}
    if host in hosts:
        return True
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    wanted = {str(info[4][0]) for info in infos}
    return bool(wanted) and wanted <= hosts


def _redact_uvicorn(known: Callable[[], Iterable[str]]) -> None:
    """Redact secrets from uvicorn's own log records (one filter, however often called)."""
    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter(known))


class _WithLifespanState:
    """Give every request the lifespan state, as uvicorn does when it runs the lifespan."""

    def __init__(self, app: ASGIApp, state: dict[str, Any]) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            scope["state"] = {**self.state, **scope.get("state", {})}
        await self.app(scope, receive, send)


class ManagerRunner:
    """Serves `app` and moves it to a new address when `server.host/port` change."""

    def __init__(self, app: FastAPI, host: str, port: int, *, log_level: str = "info") -> None:
        self.app = app
        self.state: ManagerState = app.state.manager
        self.host, self.port = host, port
        self.log_level = log_level
        self._server: _Server | None = None
        self._target: tuple[str, int] | None = None
        self._stopped = False
        self._lock = threading.Lock()
        # The pending rebind (REBIND_DELAY_S after a save), cancelled by a later save
        # that puts the address back.
        self._timer: threading.Timer | None = None
        self._signals = 0

    def on_settings_saved(self, changes: list[Change], restart_required: bool) -> None:
        if not any(c.key in ("server.host", "server.port") for c in changes):
            return
        server = self.state.settings.current.global_.server
        target = (server.host, server.port)
        with self._lock:
            current = self._server
            # The saved value can differ from the previous setting yet equal where the
            # manager listens (it was started with --port, or the setting is set back):
            # moving to the same address would only cut every SSE stream.
            if current is not None and (
                listening_on(current, *target)
                or (target == (self.host, self.port) and current.started)
            ):
                self._target = None
                if self._timer is not None:
                    self._timer.cancel()  # a change and change-back: stay where we are
                    self._timer = None
                log.info("server.host/port saved as %s:%s, already bound there", *target)
                return
            self._target = target
            if current is not None:
                if self._timer is not None:
                    self._timer.cancel()
                self._timer = threading.Timer(REBIND_DELAY_S, self._exit, args=(current,))
                self._timer.daemon = True
                self._timer.start()

    def _exit(self, server: _Server) -> None:
        """Stop `server` for a rebind, only if a rebind is still wanted: a timer that
        fires after the address was put back must not stop the only listener."""
        with self._lock:
            self._timer = None
            if self._target is None or self._server is not server:
                return
        server.should_exit = True

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            current = self._server
        if current is not None:
            current.should_exit = True

    def handle_signal(self, number: int) -> None:
        """SIGINT/SIGTERM: stop. A repeat (`uv run` forwards the copy it gets, the menu
        bar app follows SIGINT with SIGTERM) is only logged: the HTTP drain is bounded by
        GRACEFUL_SHUTDOWN_S, and the lifespan shutdown after it restores the app
        integrations (SPEC §11.4) and stops the engine, so it must not be cut short."""
        self._signals += 1
        name = signal.Signals(number).name
        if self._signals == 1:
            log.info("%s received; stopping", name)
            self.stop()
        else:
            log.info("%s received again; still stopping", name)

    def request_stop(self) -> None:
        """POST /api/admin/shutdown (`splash stop`, `splash restart`, SPEC §12.2): the same
        stop as one signal (§4.2). The route answers first; the drain and the lifespan
        shutdown follow, so the process exits 0 once the restore has run."""
        log.info("stop requested through the admin API; stopping")
        self.stop()

    @contextlib.contextmanager
    def _signal_handlers(self) -> Iterator[None]:
        """Hold SIGINT and SIGTERM from the lifespan startup to the end of its shutdown.
        uvicorn alone put the default handlers back as its server returned, so a second
        signal during the restore (`uv run` forwards its own copy, the menu bar app
        escalates to SIGTERM) killed the manager with 143 before restoring anything;
        under the LaunchAgent, whose KeepAlive restarts a failed exit, a new manager
        then took the port. The loop's wakeup fd also wakes the loop at once, whichever
        thread took the signal."""
        if threading.current_thread() is not threading.main_thread():
            yield  # tests run the runner on a thread; they stop it with stop()
            return
        loop = asyncio.get_running_loop()
        handled = (signal.SIGINT, signal.SIGTERM)
        for number in handled:
            loop.add_signal_handler(number, self.handle_signal, number)
        try:
            yield
        finally:
            for number in handled:
                loop.remove_signal_handler(number)

    async def serve(self) -> None:
        self.state.settings_listeners.append(self.on_settings_saved)
        self.state.request_shutdown = self.request_stop
        try:
            with self._signal_handlers():
                async with self.app.router.lifespan_context(self.app) as lifespan_state:
                    app = _WithLifespanState(self.app, dict(lifespan_state or {}))
                    await self._serve_loop(app)
        finally:
            self.state.request_shutdown = None
            self.state.settings_listeners.remove(self.on_settings_saved)
            self.state.bound = None

    def _finish(self) -> tuple[str, int] | None:
        """Where to listen next after the server exited, or None to stop."""
        with self._lock:
            self._server = None
            return None if self._stopped else self._target

    async def _serve_loop(self, app: ASGIApp) -> None:
        while True:
            config = uvicorn.Config(
                app,
                host=self.host,
                port=self.port,
                log_level=self.log_level,
                access_log=False,
                lifespan="off",
                timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
                # The Codex router's Responses WebSocket carries whole requests (D63).
                ws_max_size=MAX_BODY,
            )
            # uvicorn logs each WebSocket handshake with its path, which holds the
            # Codex router token (D58); its logger is outside the `splash_gui` tree.
            _redact_uvicorn(self.state.secrets.known_values)
            server = _Server(config)
            with self._lock:
                if self._stopped:
                    return
                self._server, self._target = server, None
            self.state.bound = (self.host, self.port)
            log.info("Splashboard manager %s listening on %s:%s", __version__, self.host, self.port)
            await server.serve()
            target = self._finish()
            if target is None:
                return
            if not server.started:
                log.error("could not listen on %s:%s; stopping", self.host, self.port)
                return
            self.host, self.port = target
            log.info("rebinding to %s:%s (settings changed)", *target)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="splash-gui-manager", description=__doc__)
    parser.add_argument("--host", help="override server.host for this run")
    parser.add_argument("--port", type=int, help="override server.port for this run")
    parser.add_argument("--web-dist", type=Path, help="directory of the built web admin")
    parser.add_argument(
        "--log-level", default="info", choices=("debug", "info", "warning", "error")
    )
    parser.add_argument("--console", action="store_true", help="also log to stderr")
    parser.add_argument("--version", action="version", version=f"Splashboard {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = Paths.from_env().ensure()
    secrets = SecretStore(backend_from_env(paths))
    setup_logging(
        paths,
        level=getattr(logging, args.log_level.upper()),
        known_secrets=secrets.known_values,
        console=args.console,
    )
    settings = SettingsStore(paths)
    doc = settings.load()
    host = args.host or doc.global_.server.host
    port = args.port or doc.global_.server.port
    try:
        check_bind(
            host,
            secrets,
            doc.global_.security.api_key_required,
            doc.global_.security.admin_requires_key,
        )
        with instance_lock(paths):
            app = create_app(
                AppConfig(paths=paths, web_dist=args.web_dist, secrets=secrets, settings=settings)
            )
            runner = ManagerRunner(app, host, port, log_level=args.log_level)
            with contextlib.suppress(KeyboardInterrupt):
                asyncio.run(runner.serve())
    except StartupError as error:
        log.error("%s", error)
        print(f"splash-gui-manager: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

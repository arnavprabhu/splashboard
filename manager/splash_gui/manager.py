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
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import Any

import uvicorn
from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .app import AppConfig, create_app
from .logging_setup import setup_logging
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
            raise StartupError(f"Splash GUI manager is already running (PID {pid})") from None
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
    """A uvicorn server that records a stop signal instead of re-raising it on exit."""

    stop_requested = False

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        handled = (signal.SIGINT, signal.SIGTERM)
        original = {sig: signal.signal(sig, self.handle_exit) for sig in handled}
        try:
            yield
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self.stop_requested = True
        super().handle_exit(sig, frame)


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

    def on_settings_saved(self, changes: list[Change], restart_required: bool) -> None:
        if not any(c.key in ("server.host", "server.port") for c in changes):
            return
        server = self.state.settings.current.global_.server
        with self._lock:
            self._target = (server.host, server.port)
            current = self._server
        if current is not None:
            threading.Timer(REBIND_DELAY_S, self._exit, args=(current,)).start()

    @staticmethod
    def _exit(server: _Server) -> None:
        server.should_exit = True

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            current = self._server
        if current is not None:
            current.should_exit = True

    async def serve(self) -> None:
        self.state.settings_listeners.append(self.on_settings_saved)
        try:
            async with self.app.router.lifespan_context(self.app) as lifespan_state:
                app = _WithLifespanState(self.app, dict(lifespan_state or {}))
                await self._serve_loop(app)
        finally:
            self.state.settings_listeners.remove(self.on_settings_saved)
            self.state.bound = None

    def _finish(self, server: _Server) -> tuple[str, int] | None:
        """Where to listen next after `server` exited, or None to stop."""
        with self._lock:
            self._server = None
            return None if (server.stop_requested or self._stopped) else self._target

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
            )
            server = _Server(config)
            with self._lock:
                if self._stopped:
                    return
                self._server, self._target = server, None
            self.state.bound = (self.host, self.port)
            log.info("Splash GUI manager %s listening on %s:%s", __version__, self.host, self.port)
            await server.serve()
            target = self._finish(server)
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
    parser.add_argument("--version", action="version", version=f"Splash GUI {__version__}")
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

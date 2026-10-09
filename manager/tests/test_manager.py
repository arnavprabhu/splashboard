"""`splash-gui-manager` startup checks."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

from splash_gui import manager
from splash_gui.app import AppConfig, create_app
from splash_gui.paths import Paths
from splash_gui.secrets import SecretName, SecretStore

from .conftest import mode
from .fakeengine import FAKE_BIN


def test_loopback_bind_needs_no_key(secrets: SecretStore) -> None:
    manager.check_bind("127.0.0.1", secrets, require_key=False)
    manager.check_bind("localhost", secrets, require_key=False)


def test_lan_bind_refused_without_key(secrets: SecretStore) -> None:
    with pytest.raises(manager.StartupError, match="without an API key"):
        manager.check_bind("0.0.0.0", secrets, require_key=True)  # noqa: S104
    secrets.generate(SecretName.API_KEY)
    with pytest.raises(manager.StartupError):
        manager.check_bind("0.0.0.0", secrets, require_key=False)  # noqa: S104
    manager.check_bind("0.0.0.0", secrets, require_key=True)  # noqa: S104


def test_lan_bind_refused_without_admin_sign_in(secrets: SecretStore) -> None:
    """For a settings.json written before the rule existed."""
    secrets.generate(SecretName.API_KEY)
    with pytest.raises(manager.StartupError, match="admin sign-in off"):
        manager.check_bind("0.0.0.0", secrets, require_key=True, admin_requires_key=False)  # noqa: S104
    manager.check_bind("0.0.0.0", secrets, require_key=True, admin_requires_key=True)  # noqa: S104
    manager.check_bind("127.0.0.1", secrets, require_key=False, admin_requires_key=False)


def test_instance_lock_writes_pid(paths: Paths) -> None:
    with manager.instance_lock(paths):
        assert paths.manager_pid.read_text().strip().isdigit()
        assert mode(paths.manager_pid) == 0o600
        with (
            pytest.raises(manager.StartupError, match="already running"),
            manager.instance_lock(paths),
        ):
            pass
    assert not paths.manager_pid.exists()


def test_main_runs_the_runner_on_settings_bind(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, Any] = {}

    async def fake_serve(self: manager.ManagerRunner) -> None:
        calls.update(host=self.host, port=self.port, pid_during_run=paths.manager_pid.exists())

    monkeypatch.setattr(manager.ManagerRunner, "serve", fake_serve)
    assert manager.main(["--port", "8123"]) == 0
    assert calls["host"] == "127.0.0.1" and calls["port"] == 8123
    assert calls["pid_during_run"] is True
    assert not paths.manager_pid.exists()


def test_main_refuses_lan_without_key(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    async def must_not_serve(self: manager.ManagerRunner) -> None:
        pytest.fail("must not run")

    monkeypatch.setattr(manager.ManagerRunner, "serve", must_not_serve)
    assert manager.main(["--host", "0.0.0.0"]) == 1  # noqa: S104


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for(url: str, timeout: float = 10.0) -> httpx.Response:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return httpx.get(url, timeout=1)
        except httpx.TransportError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def test_port_change_rebinds_without_restarting_the_lifespan(
    paths: Paths, secrets: SecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager, "REBIND_DELAY_S", 0.05)
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[dict[str, Any]]:
        events.append("startup")
        yield {"marker": "lifespan-state"}
        events.append("shutdown")

    app = create_app(AppConfig(paths=paths, secrets=secrets))
    app.router.lifespan_context = lifespan

    @app.get("/marker")
    def marker(request: Request) -> dict[str, Any]:
        return {"marker": request.state.marker}

    first, second = _free_port(), _free_port()
    runner = manager.ManagerRunner(app, "127.0.0.1", first, log_level="warning")
    thread = threading.Thread(target=lambda: asyncio.run(runner.serve()), daemon=True)
    thread.start()
    try:
        assert _wait_for(f"http://127.0.0.1:{first}/marker").json() == {"marker": "lifespan-state"}
        assert app.state.manager.bound == ("127.0.0.1", first)
        token = app.state.manager.auth.cli_token()
        headers = {"Authorization": f"Bearer {token}"}
        base = f"http://127.0.0.1:{first}/api/admin"
        doc = httpx.get(f"{base}/settings", headers=headers).json()["settings"]
        doc["global"]["server"]["port"] = second
        saved = httpx.put(f"{base}/settings", json=doc, headers=headers)
        assert saved.status_code == 200, saved.text
        assert {"key": "server.port", "model": None, "applies": "immediate"} in saved.json()[
            "changed"
        ]
        assert _wait_for(f"http://127.0.0.1:{second}/health").status_code == 200
        with pytest.raises(httpx.TransportError):
            httpx.get(f"http://127.0.0.1:{first}/health", timeout=1)
        assert events == ["startup"]
        assert app.state.manager.bound == ("127.0.0.1", second)
    finally:
        runner.stop()
        thread.join(timeout=15)
    assert not thread.is_alive()
    assert events == ["startup", "shutdown"]


def test_port_change_to_a_busy_port_is_refused(client: TestClient) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        client.app.state.manager.bound = ("127.0.0.1", 8000)  # type: ignore[attr-defined]
        doc = client.get("/api/admin/settings").json()["settings"]
        doc["global"]["server"]["port"] = port
        response = client.put("/api/admin/settings", json=doc)
    assert response.status_code == 422
    issue = response.json()["error"]["issues"][0]
    assert issue["code"] == "port_in_use" and issue["key"] == "server.port"
    assert client.get("/api/admin/settings").json()["settings"]["global"]["server"]["port"] == 8000


def test_a_manager_started_with_port_reports_where_it_listens(client: TestClient) -> None:
    """Acceptance 1.3 F2: started with `--port 8151` while `server.port` is 8000 (where
    oMLX runs on the user's Mac), the wizard must show 8151 as current and must not
    propose a busy port. GET /settings says where the manager listens, and validation
    probes any other address, the stored one included."""
    with socket.socket() as busy, socket.socket() as ours:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        ours.bind(("127.0.0.1", 0))
        bound = ours.getsockname()[1]
        client.app.state.manager.bound = ("127.0.0.1", bound)  # type: ignore[attr-defined]
        doc = client.get("/api/admin/settings").json()
        assert doc["listening_port"] == bound
        settings = doc["settings"]
        # The stored port is someone else's (here: the busy socket's).
        settings["global"]["server"]["port"] = taken
        client.app.state.manager.settings.current.global_.server.port = taken  # type: ignore[attr-defined]

        same = client.post("/api/admin/settings/validate", json=settings).json()
        assert same["valid"] is True, "a save that keeps the stored port still works"
        assert [w["code"] for w in same["warnings"]] == ["port_in_use"]

        settings["global"]["server"]["port"] = bound
        assert client.post("/api/admin/settings/validate", json=settings).json() == {
            "valid": True,
            "errors": [],
            "warnings": [],
        }, "the port we listen on is never probed"

    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        busy_port = other.getsockname()[1]
        settings["global"]["server"]["port"] = busy_port
        result = client.post("/api/admin/settings/validate", json=settings).json()
        assert result["valid"] is False
        assert [e["code"] for e in result["errors"]] == ["port_in_use"]


def test_saving_the_address_already_bound_does_not_rebind(
    paths: Paths, secrets: SecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug 6: the manager was started with --port (settings.json held another port);
    saving server.port = the bound port is a settings change, but the address is the
    same, so the server must not restart and cut every SSE stream."""
    monkeypatch.setattr(manager, "REBIND_DELAY_S", 0.05)
    app = create_app(AppConfig(paths=paths, secrets=secrets))
    port = _free_port()
    assert app.state.manager.settings.current.global_.server.port != port
    runner = manager.ManagerRunner(app, "127.0.0.1", port, log_level="warning")
    thread = threading.Thread(target=lambda: asyncio.run(runner.serve()), daemon=True)
    thread.start()
    try:
        assert _wait_for(f"http://127.0.0.1:{port}/health").status_code == 200
        server = runner._server
        assert server is not None and manager.listening_on(server, "127.0.0.1", port)
        token = app.state.manager.auth.cli_token()
        headers = {"Authorization": f"Bearer {token}"}
        base = f"http://127.0.0.1:{port}/api/admin"
        doc = httpx.get(f"{base}/settings", headers=headers).json()["settings"]
        doc["global"]["server"]["port"] = port
        saved = httpx.put(f"{base}/settings", json=doc, headers=headers)
        assert saved.status_code == 200, saved.text
        assert any(c["key"] == "server.port" for c in saved.json()["changed"])
        time.sleep(0.5)  # well past REBIND_DELAY_S
        assert runner._server is server and not server.should_exit, "no rebind"
        assert httpx.get(f"http://127.0.0.1:{port}/health").status_code == 200
    finally:
        runner.stop()
        thread.join(timeout=15)
    assert not thread.is_alive()


def test_listening_on_compares_bound_sockets() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

        listener = type("Listener", (), {})()
        listener.sockets = [sock]
        server: Any = type("Server", (), {})()
        server.servers = [listener]
        assert manager.listening_on(server, "127.0.0.1", port)
        assert manager.listening_on(server, "127.0.0.1", port + 1) is False
        assert manager.listening_on(server, "0.0.0.0", port) is False  # noqa: S104
        assert manager.bound_addresses(server) == {("127.0.0.1", port)}


def test_port_change_and_change_back_keeps_the_manager_listening(
    paths: Paths, secrets: SecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding: saving port X and then the bound port again within
    REBIND_DELAY_S let the first save's timer stop the server after the second save
    had cleared the target, so the manager exited with nothing listening."""
    monkeypatch.setattr(manager, "REBIND_DELAY_S", 0.4)
    app = create_app(AppConfig(paths=paths, secrets=secrets))
    port, other = _free_port(), _free_port()
    runner = manager.ManagerRunner(app, "127.0.0.1", port, log_level="warning")
    thread = threading.Thread(target=lambda: asyncio.run(runner.serve()), daemon=True)
    thread.start()
    try:
        assert _wait_for(f"http://127.0.0.1:{port}/health").status_code == 200
        server = runner._server
        assert server is not None
        token = app.state.manager.auth.cli_token()
        headers = {"Authorization": f"Bearer {token}"}
        base = f"http://127.0.0.1:{port}/api/admin"
        doc = httpx.get(f"{base}/settings", headers=headers).json()["settings"]
        for value in (other, port):
            doc["global"]["server"]["port"] = value
            saved = httpx.put(f"{base}/settings", json=doc, headers=headers)
            assert saved.status_code == 200, saved.text
        time.sleep(1.0)  # well past the first save's timer
        assert thread.is_alive(), "the manager must not exit"
        assert runner._server is server and not server.should_exit
        assert httpx.get(f"http://127.0.0.1:{port}/health").status_code == 200
    finally:
        runner.stop()
        thread.join(timeout=15)
    assert not thread.is_alive()


def test_a_late_rebind_timer_does_nothing_once_the_target_is_cleared(
    paths: Paths, secrets: SecretStore
) -> None:
    app = create_app(AppConfig(paths=paths, secrets=secrets))
    runner = manager.ManagerRunner(app, "127.0.0.1", _free_port(), log_level="warning")
    server: Any = type("Server", (), {"should_exit": False})()
    runner._server = server
    runner._target = None
    runner._exit(server)
    assert server.should_exit is False
    runner._target = ("127.0.0.1", 1)
    runner._exit(server)
    assert server.should_exit is True


# A manager process whose lifespan shutdown stands in for the integration restore
#: it takes a moment and leaves a marker when it has finished. argv: port,
# marker path, and the signal to send itself during the restore ("" for none).
_STOPPABLE = """
import asyncio, contextlib, os, signal, sys
from pathlib import Path
from fastapi import FastAPI, WebSocket
from splash_gui import manager
from splash_gui.app import AppConfig, create_app
from splash_gui.paths import Paths
from splash_gui.secrets import MemoryBackend, SecretStore

port, marker, late = int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
app = create_app(AppConfig(paths=Paths.from_env().ensure(), secrets=SecretStore(MemoryBackend())))

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    yield {}
    if late:
        os.kill(os.getpid(), getattr(signal, late))
    await asyncio.sleep(0.3)
    marker.write_text("restored")

app.router.lifespan_context = lifespan

@app.websocket("/hold")
async def hold(websocket: WebSocket) -> None:
    await websocket.accept()
    while True:
        await websocket.receive_text()

with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(manager.ManagerRunner(app, "127.0.0.1", port, log_level="warning").serve())
"""


def _start_stoppable(tmp_path: Path, late: str = "") -> tuple[subprocess.Popen[bytes], int, Path]:
    port, marker = _free_port(), tmp_path / "restored"
    # The fake engine, so discovery never reaches the venv's own `splash` entry point.
    env = {
        **os.environ,
        "SPLASH_GUI_REAL_SPLASH": str(FAKE_BIN),
        "FAKE_SPLASH_PYTHON": sys.executable,
    }
    process = subprocess.Popen(
        [sys.executable, "-c", _STOPPABLE, str(port), str(marker), late], env=env
    )
    try:
        _wait_for(f"http://127.0.0.1:{port}/health")
    except BaseException:
        process.kill()
        raise
    return process, port, marker


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_during_the_shutdown_restore_does_not_cut_it_short(
    tmp_path: Path, number: signal.Signals
) -> None:
    """A second signal used to find uvicorn's handlers gone once its server returned:
    SIGTERM killed the manager with 143 before the restore (launchd's KeepAlive then
    started it again) and SIGINT cancelled the restore."""
    process, _, marker = _start_stoppable(tmp_path, late=number.name)
    try:
        process.send_signal(number)
        assert process.wait(timeout=10) == 0
    finally:
        process.kill()
    assert marker.read_text() == "restored"


def test_one_sigterm_stops_the_manager_with_a_websocket_open(tmp_path: Path) -> None:
    """A SIGTERM sent to `uv run` and its child at once reaches the manager twice (uv
    forwards its copy); the WebSocket is closed with 1012 and the restore still runs."""
    process, port, marker = _start_stoppable(tmp_path)
    try:
        with ws_connect(f"ws://127.0.0.1:{port}/hold") as websocket:
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            process.send_signal(signal.SIGTERM)
            with pytest.raises(ConnectionClosed) as closed:
                websocket.recv(timeout=5)
            assert closed.value.rcvd is not None and closed.value.rcvd.code == 1012
            assert process.wait(timeout=5) == 0
            assert time.monotonic() - started < 3
    finally:
        process.kill()
    assert marker.read_text() == "restored"
    with pytest.raises(httpx.TransportError):
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)

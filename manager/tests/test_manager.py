"""`splash-gui-manager` startup checks (SPEC §4.2, §17.1)."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from splash_gui import manager
from splash_gui.app import AppConfig, create_app
from splash_gui.paths import Paths
from splash_gui.secrets import SecretName, SecretStore

from .conftest import mode


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
    """D42, for a settings.json written before the rule existed."""
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

"""`splash stop` and `splash restart` against a real manager (SPEC §12.2, §4.2), and the
admin route they call, `POST /api/admin/shutdown`. Until this route existed both commands
failed with "Not Found" and left the manager, the engine and the integrations running."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui import cli as cli_module
from splash_gui import manager
from splash_gui.app import AppConfig, create_app
from splash_gui.auth.core import AuthManager
from splash_gui.paths import Paths
from splash_gui.secrets import MemoryBackend, SecretStore

from .fakeengine import FAKE_BIN
from .test_manager import _free_port, _wait_for


def test_the_shutdown_route_stops_the_runner_and_runs_the_lifespan_shutdown(
    paths: Paths, secrets: SecretStore
) -> None:
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[dict[str, Any]]:
        events.append("startup")
        yield {}
        events.append("shutdown")

    app = create_app(AppConfig(paths=paths, secrets=secrets))
    app.router.lifespan_context = lifespan
    port = _free_port()
    runner = manager.ManagerRunner(app, "127.0.0.1", port, log_level="warning")
    thread = threading.Thread(target=lambda: asyncio.run(runner.serve()), daemon=True)
    thread.start()
    try:
        _wait_for(f"http://127.0.0.1:{port}/health")
        token = app.state.manager.auth.cli_token()
        reply = httpx.post(
            f"http://127.0.0.1:{port}/api/admin/shutdown",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert reply.status_code == 202 and reply.json() == {"ok": True}
        thread.join(timeout=15)
    finally:
        runner.stop()
        thread.join(timeout=15)
    assert not thread.is_alive()
    assert events == ["startup", "shutdown"]
    assert app.state.manager.request_shutdown is None, "the hook is cleared when the runner ends"


def test_the_shutdown_route_needs_the_cli_token(paths: Paths, secrets: SecretStore) -> None:
    app = create_app(AppConfig(paths=paths, secrets=secrets))
    with TestClient(app, client=("127.0.0.1", 50000)) as anonymous:
        # Refused by the admin guard (SPEC §7.2): loopback alone is never a credential.
        assert anonymous.post("/api/admin/shutdown").status_code in (401, 403)


def test_the_shutdown_route_says_so_when_no_runner_serves_the_app(client: TestClient) -> None:
    reply = client.post("/api/admin/shutdown")
    assert reply.status_code == 503
    assert reply.json()["error"]["code"] == "shutdown_unavailable"


# A manager process as `splash-gui-manager` runs it: under the instance lock, serving through
# ManagerRunner. Its lifespan shutdown stands in for the integration restore (SPEC §11.4): it
# takes a moment and then leaves a marker. argv: port, marker path.
_MANAGER = """
import asyncio, contextlib, sys
from pathlib import Path
from fastapi import FastAPI
from splash_gui import manager
from splash_gui.app import AppConfig, create_app
from splash_gui.paths import Paths
from splash_gui.secrets import MemoryBackend, SecretStore

port, marker = int(sys.argv[1]), Path(sys.argv[2])
paths = Paths.from_env().ensure()
app = create_app(AppConfig(paths=paths, secrets=SecretStore(MemoryBackend())))

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    yield {}
    await asyncio.sleep(0.5)
    marker.write_text("restored")

app.router.lifespan_context = lifespan
with manager.instance_lock(paths):
    asyncio.run(manager.ManagerRunner(app, "127.0.0.1", port, log_level="warning").serve())
"""


def _ensure_cli_token(paths: Paths) -> None:
    """The CLI reads the token file; a manager subprocess does not create it."""
    AuthManager(paths, SecretStore(MemoryBackend())).ensure_cli_token()


def _start_manager(tmp_path: Path) -> tuple[subprocess.Popen[bytes], int, Path]:
    port, marker = _free_port(), tmp_path / "restored"
    # The fake engine, so discovery never reaches a real Splash on this Mac.
    env = {
        **os.environ,
        "SPLASH_GUI_REAL_SPLASH": str(FAKE_BIN),
        "FAKE_SPLASH_PYTHON": sys.executable,
    }
    process = subprocess.Popen([sys.executable, "-c", _MANAGER, str(port), str(marker)], env=env)
    try:
        _wait_for(f"http://127.0.0.1:{port}/health")
    except BaseException:
        process.kill()
        raise
    return process, port, marker


def test_splash_stop_returns_only_after_the_manager_has_restored_and_exited(
    paths: Paths, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ensure_cli_token(paths)
    process, port, marker = _start_manager(tmp_path)
    try:
        code = cli_module.main(["stop", "--port", str(port)])
        out, err = capsys.readouterr()
        assert code == 0, err
        assert "Splash GUI stopped" in out
        # The lock is free only after the lifespan shutdown, so the restore has run.
        assert marker.read_text() == "restored"
        assert process.wait(timeout=10) == 0
    finally:
        process.kill()
    with pytest.raises(httpx.TransportError):
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)


def test_splash_restart_starts_the_manager_only_after_the_old_one_has_restored(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ensure_cli_token(paths)
    process, port, marker = _start_manager(tmp_path)
    seen: list[tuple[int, bool, bool]] = []

    def fake_start(self: Any, foreground: bool = False, *, note: bool = True) -> None:
        # Recorded instead of spawning a second manager: at this point the old one must
        # have restored and released its lock, or the new one would fail to start.
        seen.append((self.port, marker.exists(), cli_module.manager_exited(paths)))

    monkeypatch.setattr(cli_module.Client, "start", fake_start)
    try:
        assert cli_module.main(["restart", "--port", str(port)]) == 0
        assert process.wait(timeout=10) == 0
    finally:
        process.kill()
    assert seen == [(port, True, True)]


def test_splash_stop_says_so_when_nothing_is_running(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli_module.main(["stop", "--port", str(_free_port())])
    out, err = capsys.readouterr()
    assert code == 0 and "not running" in err and out == ""

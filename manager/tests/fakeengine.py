"""Running the manager against the fake Splash engine (scripts/fake_splash).

`EngineHarness` owns a TestClient whose app discovers the fake package, roots every
engine/installer path under the test's SPLASH_GUI_HOME, and shrinks the
supervisor's timings so state-machine tests run in seconds.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.app import AppConfig, create_app
from splash_gui.engine.discovery import discover
from splash_gui.paths import Paths
from splash_gui.secrets import MemoryBackend, SecretStore

REPO = Path(__file__).resolve().parents[2]
FAKE = REPO / "scripts" / "fake_splash"
FAKE_PKG = FAKE / "pkg"
FAKE_BIN = FAKE_PKG / "bin" / "splash"
FAKE_INSTALLER = FAKE_PKG / "install" / "models.py"
MODEL = "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"
MODEL_27B = "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M"
LOOPBACK = ("127.0.0.1", 50001)


def fake_environment(monkeypatch: pytest.MonkeyPatch, home: Path, **extra: str) -> Path:
    """Environment the fake engine and installer inherit through the manager."""
    data = home / "fake-data"
    monkeypatch.setenv("SPLASH_GUI_FAKE_DATA", str(data))
    monkeypatch.setenv("FAKE_SPLASH_PYTHON", sys.executable)
    monkeypatch.setenv("PYTHONUNBUFFERED", "1")
    monkeypatch.setenv("FAKE_SPLASH_LOAD_SECONDS", "0.05")
    monkeypatch.setenv("FAKE_SPLASH_TOKS", "2000")
    monkeypatch.setenv("FAKE_SPLASH_FLUSH_SECONDS", "0.05")
    monkeypatch.setenv("FAKE_SPLASH_KEEPALIVE_SECONDS", "0.5")
    monkeypatch.setenv("SPLASH_GUI_OSASCRIPT", "0")
    monkeypatch.setenv("SPLASH_GUI_UPDATE_CHECK", "0")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    return data


def install(home: Path, model: str = MODEL, *args: str, env: dict[str, str] | None = None) -> None:
    """Install `model` with the fake installer, where the manager looks for it."""
    environment = {
        **os.environ,
        "HF_HUB_CACHE": str(home / "models"),
        "FAKE_SPLASH_PYTHON": sys.executable,
        **(env or {}),
    }
    result = subprocess.run(
        [
            str(FAKE_PKG / "python" / "bin" / "python3"),
            str(FAKE_INSTALLER),
            "--model",
            model,
            *args,
            "prepare",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


class EngineHarness:
    def __init__(self, app: FastAPI, client: TestClient, home: Path) -> None:
        self.app = app
        self.client = client
        self.home = home

    @property
    def state(self) -> Any:
        return self.app.state.manager

    @property
    def sup(self) -> Any:
        return self.state.supervisor

    def engine(self) -> dict[str, Any]:
        response = self.client.get("/api/admin/engine")
        assert response.status_code == 200, response.text
        return dict(response.json())

    def wait_state(self, *states: str, timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        view = self.engine()
        while view["state"] not in states:
            if time.monotonic() > deadline:
                raise AssertionError(f"engine state {view['state']} never became {states}: {view}")
            time.sleep(0.05)
            view = self.engine()
        return view

    def load(self, model: str = MODEL, **body: Any) -> dict[str, Any]:
        response = self.client.post("/api/admin/engine/load", json={"model": model, **body})
        assert response.status_code == 202, response.text
        return self.wait_state("ready", "failed", timeout=30)

    def fake(self, method: str, path: str, body: Any = None) -> Any:
        """Call the fake engine's control endpoints on its internal port."""
        port = self.sup.internal_port
        assert port, "engine not running"
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
        except OSError:
            return None
        return json.loads(raw) if raw else None

    def last_engine_requests(self) -> list[dict[str, Any]]:
        return list(self.fake("GET", "/_fake/last_requests")["requests"])

    def settings_document(self) -> dict[str, Any]:
        response = self.client.get("/api/admin/settings")
        assert response.status_code == 200, response.text
        return dict(response.json()["settings"])

    def patch_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge `patch` into the stored settings and save.

        `PUT /settings` replaces the whole document (it is settings.json), so a
        partial PUT would silently drop every other section.
        """
        document = self.settings_document()

        def merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
            for key, value in source.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    merge(target[key], value)
                else:
                    target[key] = value
            return target

        response = self.client.put("/api/admin/settings", json=merge(document, patch))
        assert response.status_code == 200, response.text
        return dict(response.json())

    def set_installed(self, *models: str) -> None:
        installed = frozenset(models)
        self.state.installed_models = lambda: installed


def tune(app: FastAPI) -> None:
    sup = app.state.manager.supervisor
    sup.stop_timeout_s = 5.0
    sup.second_sigint_s = 2.0
    sup.backoff_s = (0.05, 0.1, 0.2)
    sup.ready_poll_s = 0.1
    sup.ready_poll_after_line_s = 0.02
    sup.status_fast_s = 0.2
    sup.status_slow_s = 0.2
    sup.idle_check_s = 3600.0


@pytest.fixture
def harness_factory(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Callable[..., EngineHarness]]:
    clients: list[TestClient] = []

    def make(
        installed: tuple[str, ...] | None = (MODEL,),
        env: dict[str, str] | None = None,
        configure: Callable[[FastAPI], None] | None = None,
        headers: dict[str, str] | None = None,
        base_url: str = "http://127.0.0.1:8000",
    ) -> EngineHarness:
        fake_environment(monkeypatch, isolated_home, **(env or {}))
        paths = Paths(isolated_home).ensure()
        for model in installed or ():
            install(isolated_home, model)
        app = create_app(
            AppConfig(paths=paths, web_dist=tmp_path / "dist", secrets=SecretStore(MemoryBackend()))
        )
        engine = discover(str(FAKE_BIN), prefix=None)
        assert engine.found and engine.version == "1.3.1", engine
        app.state.manager.discover_engine = lambda: engine
        # Never the developer's real home: shell rc files, ~/.hermes, ~/.pi, app configs.
        app.state.manager.user_home = tmp_path / "user-home"
        tune(app)
        if configure is not None:
            configure(app)
        token = app.state.manager.auth.cli_token()
        client = TestClient(
            app,
            base_url=base_url,
            client=LOOPBACK,
            headers={"Authorization": f"Bearer {token}", **(headers or {})},
        )
        client.__enter__()
        clients.append(client)
        harness = EngineHarness(app, client, isolated_home)
        if installed is not None:
            harness.set_installed(*installed)
        return harness

    yield make
    for client in reversed(clients):
        client.__exit__(None, None, None)

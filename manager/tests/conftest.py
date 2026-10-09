from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui import paths as paths_module
from splash_gui.app import AppConfig, create_app
from splash_gui.engine.discovery import EngineInfo
from splash_gui.paths import Paths
from splash_gui.secrets import MemoryBackend, SecretStore

SPLASH_PKG = Path("/opt/homebrew/opt/splash/libexec")
SPLASH_PYTHON = SPLASH_PKG / "python" / "bin" / "python3"
HAVE_SPLASH = SPLASH_PYTHON.exists() and (SPLASH_PKG / "server" / "serve_options.py").exists()

REPO = Path(__file__).resolve().parents[2]
FAKE_SPLASH = REPO / "scripts" / "fake_splash"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own SPLASH_GUI_HOME and in-memory secrets: never ~/.splash."""
    home = tmp_path / "splash-home"
    monkeypatch.setenv("SPLASH_GUI_HOME", str(home))
    monkeypatch.setenv("SPLASH_GUI_SECRETS", "memory")
    monkeypatch.setenv("SPLASH_GUI_FAKE_DATA", str(home / "fake-data"))
    monkeypatch.setenv("SPLASH_GUI_UPDATE_CHECK", "0")
    # No real macOS notifications from tests: with no menu bar app connected, an
    # alert would otherwise fall back to `osascript display notification`.
    monkeypatch.setenv("SPLASH_GUI_OSASCRIPT", "0")
    monkeypatch.delenv("SPLASH_GUI_REAL_SPLASH", raising=False)
    monkeypatch.delenv("SPLASH_GUI_WEB_DIST", raising=False)
    # The developer's own `hf auth login` token must never be visible to a test
    # (the token lookup reads ~/.cache/huggingface/token), or a gated-repository test would
    # pass or fail depending on whose machine it runs on.
    monkeypatch.setenv("HF_HOME", str(home / "hf-home"))
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    # Splash's crash-trace directory is hardcoded to ~/Library/Logs/Splash/crash. A
    # test that clears traces must never delete (or litter) the developer's real ones.
    monkeypatch.setattr(paths_module, "SPLASH_CRASH_TRACE_DIR", home / "splash-crash")
    return home


_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}  # noqa: S104 - matched, never bound


@pytest.fixture
def allow_network() -> Iterator[None]:
    """Opt back in to the real internet for one test.

    Only for the real-engine contract tests, which check our
    assumptions against the installed Splash and the live Hub. Mark them with
    `@pytest.mark.real` so `make test` stays offline.
    """
    yield


@pytest.fixture(autouse=True)
def no_external_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Fail loudly if a test reaches the real Hugging Face or GitHub.

    The suite is meant to run offline against the fake engine and the fake Hub.
    A test that silently queries the live Hub passes for the wrong reason and
    fails on someone else's machine, so only loopback may resolve.
    """
    if "allow_network" in request.fixturenames:
        return
    real_create_connection = socket.create_connection

    def guarded(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if str(host) not in _LOOPBACK:
            raise AssertionError(
                f"test tried to reach {host!r}; point hf.endpoint at the fake Hub "
                "(the `fake_hub` fixture) or inject a transport"
            )
        return real_create_connection(address, *args, **kwargs)

    real_getaddrinfo = socket.getaddrinfo

    def guarded_dns(host, *args, **kwargs):
        if host is not None and str(host) not in _LOOPBACK:
            raise AssertionError(f"test tried to resolve {host!r}; this suite is offline")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guarded)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_dns)


@pytest.fixture
def paths(isolated_home: Path) -> Paths:
    return Paths(isolated_home).ensure()


@pytest.fixture
def secrets() -> SecretStore:
    return SecretStore(MemoryBackend())


@pytest.fixture
def web_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>SPA</title>")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)")
    (dist / "favicon.svg").write_text("<svg/>")
    return dist


def fake_engine() -> EngineInfo:
    return EngineInfo(
        found=True,
        cli=Path("/opt/homebrew/opt/splash/bin/splash"),
        source="brew",
        version="1.3.0",
        version_tuple=(1, 3, 0),
        support="supported",
        pkg=Path("/nonexistent/pkg"),
        python=None,
        error=None,
    )


LOOPBACK_CLIENT = ("127.0.0.1", 50000)


@pytest.fixture
def app(paths: Paths, secrets: SecretStore, web_dist: Path, tmp_path: Path) -> FastAPI:
    application = create_app(AppConfig(paths=paths, web_dist=web_dist, secrets=secrets))
    application.state.manager.discover_engine = fake_engine
    # Never the developer's real home: the shim routes edit shell rc files there,
    # and integrations read ~/.hermes, ~/.pi and desktop-app configs.
    home = tmp_path / "user-home"
    home.mkdir(exist_ok=True)
    application.state.manager.user_home = home
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """A loopback client carrying the CLI token, as the shim and menu bar app call the API."""
    token = app.state.manager.auth.cli_token()
    with TestClient(
        app, client=LOOPBACK_CLIENT, headers={"Authorization": f"Bearer {token}"}
    ) as test_client:
        yield test_client


@pytest.fixture
def browser(app: FastAPI) -> Iterator[TestClient]:
    """A same-origin browser page: no CLI token, Origin + Sec-Fetch-Site on every request."""
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=LOOPBACK_CLIENT,
        headers={"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
    ) as test_client:
        yield test_client


def write_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def which(name: str) -> str | None:
    return shutil.which(name, path=os.environ.get("PATH"))


def fake_hub_module() -> ModuleType:
    """Import scripts/fake_splash/hub.py by path, as an out-of-package script."""
    if str(FAKE_SPLASH) not in sys.path:
        sys.path.insert(0, str(FAKE_SPLASH))
    spec = importlib.util.find_spec("hub")
    assert spec and spec.origin, "scripts/fake_splash/hub.py not importable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def fake_hub_server() -> Iterator[Any]:
    """A running fake Hugging Face API and its CDN (`FakeHub`): `.url`, `.cdn_url`,
    `.requests` (every request either received), `.cdn_bps` (the CDN's rate)."""
    hub = fake_hub_module().FakeHub().start()
    try:
        yield hub
    finally:
        hub.stop()


@pytest.fixture
def fake_hub(fake_hub_server: Any) -> str:
    """A running fake Hugging Face API; its base URL.

    The manager reaches it through the `hf.endpoint` setting and the
    engine-side compatibility helper through `HF_ENDPOINT`.
    """
    return str(fake_hub_server.url)


from .fakeengine import harness_factory  # noqa: E402, F401  (fixture)

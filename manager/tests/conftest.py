from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.app import AppConfig, create_app
from splash_gui.engine.discovery import EngineInfo
from splash_gui.paths import Paths
from splash_gui.secrets import MemoryBackend, SecretStore

SPLASH_PKG = Path("/opt/homebrew/opt/splash/libexec")
SPLASH_PYTHON = SPLASH_PKG / "python" / "bin" / "python3"
HAVE_SPLASH = SPLASH_PYTHON.exists() and (SPLASH_PKG / "server" / "serve_options.py").exists()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own SPLASH_GUI_HOME and in-memory secrets: never ~/.splash."""
    home = tmp_path / "splash-home"
    monkeypatch.setenv("SPLASH_GUI_HOME", str(home))
    monkeypatch.setenv("SPLASH_GUI_SECRETS", "memory")
    monkeypatch.delenv("SPLASH_GUI_REAL_SPLASH", raising=False)
    monkeypatch.delenv("SPLASH_GUI_WEB_DIST", raising=False)
    return home


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
        version="1.2.0",
        version_tuple=(1, 2, 0),
        support="supported",
        pkg=Path("/nonexistent/pkg"),
        python=None,
        error=None,
    )


LOOPBACK_CLIENT = ("127.0.0.1", 50000)


@pytest.fixture
def app(paths: Paths, secrets: SecretStore, web_dist: Path) -> FastAPI:
    application = create_app(AppConfig(paths=paths, web_dist=web_dist, secrets=secrets))
    application.state.manager.discover_engine = fake_engine
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

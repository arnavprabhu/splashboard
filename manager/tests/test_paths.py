from __future__ import annotations

from pathlib import Path

import pytest

from splash_gui.paths import Paths, default_base, tmp_dir_for, write_atomic

from .conftest import mode


def test_base_follows_env(isolated_home: Path) -> None:
    assert default_base() == isolated_home
    assert Paths.from_env().base == isolated_home


def test_default_base_is_dot_splash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPLASH_GUI_HOME")
    assert default_base() == Path.home() / ".splash"


def test_layout_matches_spec(paths: Paths) -> None:
    base = paths.base
    assert paths.settings_file == base / "settings.json"
    assert paths.models_dir == base / "models"
    assert paths.cache_dir == base / "cache"
    assert tmp_dir_for(paths.cache_dir) == base / "cache" / "tmp"
    assert paths.chats_dir == base / "chats"
    assert paths.usage_db == base / "usage.db"
    assert paths.downloads_file == base / "downloads.json"
    assert paths.integrations_state == base / "integrations" / "state.json"
    assert paths.manager_log == base / "logs" / "manager.log"
    assert paths.engine_log == base / "logs" / "engine.log"
    assert paths.manager_pid == base / "run" / "manager.pid"
    assert paths.shim == base / "bin" / "splash"


def test_ensure_creates_private_dirs(paths: Paths) -> None:
    for directory in paths.directories():
        assert directory.is_dir()
        assert mode(directory) == 0o700, directory


def test_ensure_tightens_existing(isolated_home: Path) -> None:
    isolated_home.mkdir(parents=True)
    isolated_home.chmod(0o755)
    Paths(isolated_home).ensure()
    assert mode(isolated_home) == 0o700


def test_write_atomic_mode_and_content(tmp_path: Path) -> None:
    target = tmp_path / "x" / "file.json"
    write_atomic(target, b"one")
    write_atomic(target, b"two")
    assert target.read_bytes() == b"two"
    assert mode(target) == 0o600
    assert [p.name for p in target.parent.iterdir()] == ["file.json"]

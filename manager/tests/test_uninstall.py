"""Remove Splashboard data: `POST /uninstall/plan` and `POST /uninstall`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.cli import install as shim
from splash_gui.schemas import IntegrationError, RestoreAllResult
from splash_gui.secrets import SecretName


def _seed(app: FastAPI) -> dict[str, Path]:
    """A data folder with chats, logs, models and cache, plus the shim and a PATH block."""
    state = app.state.manager
    base: Path = state.paths.base
    (base / "chats").mkdir(parents=True, exist_ok=True)
    (base / "chats" / "a.json").write_text("{}" * 50)
    (base / "logs").mkdir(exist_ok=True)
    (base / "logs" / "manager.log").write_text("x" * 300)
    models = state.settings.models_dir()
    (models / "models--o--r" / "blobs").mkdir(parents=True, exist_ok=True)
    (models / "models--o--r" / "blobs" / "b").write_bytes(b"m" * 4000)
    cache = state.settings.cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "kv").write_bytes(b"c" * 2000)
    shim.install_shim(state.paths)
    home: Path = state.user_home
    rc = home / ".zshrc"
    rc.write_text("export A=1\n")
    shim.add_to_rc(rc, state.paths.bin_dir)
    return {"base": base, "models": models, "cache": cache, "rc": rc}


def _restore(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, errors: list[str] | None = None
) -> list[str]:
    calls: list[str] = []

    async def restore_all() -> RestoreAllResult:
        calls.append("restore_all")
        return RestoreAllResult(
            restored=[] if errors else ["claude-desktop"],
            errors=[IntegrationError(name=n, message="busy") for n in errors or []],
        )

    monkeypatch.setattr(app.state.manager.integrations, "restore_all", restore_all)
    return calls


def test_the_plan_lists_each_folder_with_its_size_and_models_and_cache_apart(
    app: FastAPI, client: TestClient
) -> None:
    seeded = _seed(app)
    plan = client.post("/api/admin/uninstall/plan").json()

    by_path = {item["path"]: item for item in plan["items"]}
    assert by_path[str(seeded["models"])]["kind"] == "models"
    assert by_path[str(seeded["cache"])]["kind"] == "cache"
    assert by_path[str(seeded["base"] / "chats")]["kind"] == "data"
    assert plan["models_bytes"] >= 4000 and plan["cache_bytes"] >= 2000
    assert plan["data_bytes"] >= 400
    assert plan["shim_installed"] is True
    assert plan["path_block_files"] == [str(seeded["rc"])]
    assert plan["steps"][0].startswith("Restore") and plan["steps"][-1] == "Stop the manager"
    assert plan["app_connected"] is False


def test_the_default_removes_the_data_and_keeps_models_and_cache(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed(app)
    calls = _restore(app, monkeypatch)
    app.state.manager.secrets.set(SecretName.HF_TOKEN, "hf_secret")

    reply = client.post("/api/admin/uninstall", json={"stop": False})

    assert reply.status_code == 200, reply.text
    result = reply.json()
    assert calls == ["restore_all"] and result["restored"] == ["claude-desktop"]
    assert seeded["rc"].read_text() == "export A=1\n"  # the block goes, byte for byte
    assert result["path_block_removed"] == [str(seeded["rc"])]
    assert result["shim_removed"] is True and not app.state.manager.paths.shim.exists()
    assert not (seeded["base"] / "chats").exists() and not (seeded["base"] / "logs").exists()
    assert (seeded["models"] / "models--o--r" / "blobs" / "b").exists()
    assert (seeded["cache"] / "kv").exists()
    assert app.state.manager.secrets.get(SecretName.HF_TOKEN) is None
    assert result["stopping"] is False
    app.state.manager.downloads.save()  # the queue saves itself on shutdown
    assert not app.state.manager.paths.downloads_file.exists()


def test_models_and_cache_go_only_when_ticked(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed(app)
    _restore(app, monkeypatch)

    result = client.post(
        "/api/admin/uninstall",
        json={"delete_models": True, "delete_cache": True, "stop": False},
    ).json()

    assert not seeded["models"].exists() and not seeded["cache"].exists()
    assert result["freed_bytes"] >= 6000


def test_a_failed_restore_stops_before_anything_is_removed(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed(app)
    _restore(app, monkeypatch, errors=["codex-app"])

    reply = client.post("/api/admin/uninstall", json={"stop": False})

    assert reply.status_code == 409
    assert reply.json()["error"]["code"] == "restore_incomplete"
    assert app.state.manager.paths.shim.exists()
    assert shim.has_block(seeded["rc"].read_text())
    assert (seeded["base"] / "chats" / "a.json").exists()


def test_a_models_folder_outside_the_data_folder_is_listed_but_never_deleted(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _restore(app, monkeypatch)
    outside = tmp_path / "my-hf-cache"
    (outside / "models--x--y").mkdir(parents=True)
    (outside / "models--x--y" / "w").write_bytes(b"w" * 100)
    state = app.state.manager
    monkeypatch.setattr(state.settings, "models_dir", lambda: outside)

    plan = client.post("/api/admin/uninstall/plan").json()
    moved = [i for i in plan["items"] if i["path"] == str(outside)]
    assert moved and moved[0]["kind"] == "models" and moved[0]["deletable"] is False

    client.post("/api/admin/uninstall", json={"delete_models": True, "stop": False})
    assert (outside / "models--x--y" / "w").exists()


def _nest(app: FastAPI, key: str, folder: Path) -> None:
    state = app.state.manager
    raw = state.settings.current.to_json_dict()
    raw["global"]["storage"][key] = str(folder)
    result, _ = state.settings.save(raw)
    assert result.ok


def test_kept_models_and_cache_nested_in_the_data_folder_survive(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _restore(app, monkeypatch)
    base: Path = app.state.manager.paths.base
    models = base / "custom" / "models"
    cache = base / "deep" / "er" / "cache"
    (models / "w.gguf").parent.mkdir(parents=True)
    (models / "w.gguf").write_bytes(b"w" * 100)
    cache.mkdir(parents=True)
    (cache / "kv").write_bytes(b"c" * 50)
    (base / "custom" / "notes.txt").write_text("data")
    (base / "deep" / "other.txt").write_text("data")
    _nest(app, "models_dir", models)
    _nest(app, "cache_dir", cache)

    plan = client.post("/api/admin/uninstall/plan").json()
    by_path = {item["path"]: item for item in plan["items"]}
    assert by_path[str(models)]["kind"] == "models" and by_path[str(models)]["deletable"]
    assert by_path[str(cache)]["kind"] == "cache" and by_path[str(cache)]["deletable"]
    assert str(base / "custom") not in by_path and str(base / "deep") not in by_path
    assert by_path[str(base / "custom" / "notes.txt")]["kind"] == "data"

    reply = client.post(
        "/api/admin/uninstall",
        json={"delete_models": False, "delete_cache": False, "stop": False},
    )

    assert reply.status_code == 200, reply.text
    assert (models / "w.gguf").exists() and (cache / "kv").exists()
    assert not (base / "custom" / "notes.txt").exists()
    assert not (base / "deep" / "other.txt").exists()


def test_nested_models_and_cache_go_when_ticked(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _restore(app, monkeypatch)
    base: Path = app.state.manager.paths.base
    models = base / "custom" / "models"
    cache = models / "cache"
    cache.mkdir(parents=True)
    (models / "w.gguf").write_bytes(b"w" * 100)
    (cache / "kv").write_bytes(b"c" * 50)
    _nest(app, "models_dir", models)
    _nest(app, "cache_dir", cache)

    client.post("/api/admin/uninstall", json={"delete_models": True, "stop": False})
    assert not (models / "w.gguf").exists() and (cache / "kv").exists()

    client.post("/api/admin/uninstall", json={"delete_cache": True, "stop": False})
    assert not cache.exists()


def test_models_named_through_a_symlink_are_found_inside_the_data_folder(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _restore(app, monkeypatch)
    base: Path = app.state.manager.paths.base
    models = base / "custom" / "models"
    models.mkdir(parents=True)
    (models / "w.gguf").write_bytes(b"w" * 100)
    alias = tmp_path / "alias"
    alias.symlink_to(base / "custom")
    _nest(app, "models_dir", alias / "models")

    client.post("/api/admin/uninstall", json={"stop": False})
    assert (models / "w.gguf").exists()


@pytest.mark.parametrize(
    ("on_disk", "typed"), [("custom/models", "Custom/Models"), ("models", "MODELS")]
)
def test_models_named_in_a_different_case_are_kept(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch, on_disk: str, typed: str
) -> None:
    _restore(app, monkeypatch)
    base: Path = app.state.manager.paths.base
    models = base / on_disk
    models.mkdir(parents=True, exist_ok=True)
    (models / "w.gguf").write_bytes(b"w" * 100)
    if not (base / typed).exists():
        pytest.skip("the filesystem is case-sensitive")
    _nest(app, "models_dir", base / typed)

    plan = client.post("/api/admin/uninstall/plan").json()
    kinds = {item["path"]: item["kind"] for item in plan["items"]}
    assert kinds.get(str(models)) == "models"
    assert all(kind != "data" or not models.is_relative_to(path) for path, kind in kinds.items())

    client.post("/api/admin/uninstall", json={"stop": False})
    assert (models / "w.gguf").exists()


def test_a_data_folder_that_is_the_home_folder_is_refused(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _restore(app, monkeypatch)
    state = app.state.manager
    monkeypatch.setattr(state, "user_home", state.paths.base)

    reply = client.post("/api/admin/uninstall", json={"stop": False})

    assert reply.status_code == 409 and reply.json()["error"]["code"] == "unsafe_home"


def test_the_manager_stops_last_when_asked(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(app)
    _restore(app, monkeypatch)
    stops: list[Any] = []
    monkeypatch.setattr(app.state.manager, "request_shutdown", lambda: stops.append(True))

    result = client.post("/api/admin/uninstall", json={"delete_data": False}).json()

    assert stops == [True] and result["stopping"] is True
    assert result["deleted"] == []


def test_uninstall_needs_a_credential(app: FastAPI, browser: TestClient) -> None:
    assert browser.post("/api/admin/uninstall").status_code == 401

"""Storage moves and imports (`storage.*`): `/storage/move`,
`/storage/import-candidates`, `/storage/import`, with their failure paths."""

from __future__ import annotations

import errno
import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.storage import api as storage_api
from splash_gui.storage.api import default_hf_hub, move_tree


def wait_job(client: TestClient, job_id: str, timeout: float = 10) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        view = client.get(f"/api/admin/jobs/{job_id}").json()
        if view["state"] != "running":
            return dict(view)
        assert time.monotonic() < deadline, view
        time.sleep(0.02)


def seed_models(root: Path) -> None:
    blob = root / "models--org--repo" / "blobs" / "abc"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"weights" * 100)
    snap = root / "models--org--repo" / "snapshots" / "c0ffee"
    snap.mkdir(parents=True)
    (snap / "model.safetensors").symlink_to("../../blobs/abc")


def storage_setting(client: TestClient, key: str) -> str:
    return str(client.get("/api/admin/settings").json()["settings"]["global"]["storage"][key])


# --- move_tree -----------------------------------------------------------------------------


def test_move_tree_same_volume_renames(tmp_path: Path) -> None:
    seed_models(tmp_path / "a")
    move_tree(tmp_path / "a", tmp_path / "b" / "models")
    assert not (tmp_path / "a").exists()
    link = tmp_path / "b" / "models" / "models--org--repo" / "snapshots" / "c0ffee"
    assert (link / "model.safetensors").is_symlink()
    assert (link / "model.safetensors").read_bytes().startswith(b"weights")


def cross_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every directory rename except staging → destination fail with EXDEV."""
    real_rename = Path.rename

    def rename(self: Path, target: Any) -> Any:
        if not self.name.endswith(storage_api.STAGING_SUFFIX):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)


def test_move_tree_across_volumes_copies_then_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_models(tmp_path / "a")
    cross_volume(monkeypatch)
    move_tree(tmp_path / "a", tmp_path / "b")
    assert not (tmp_path / "a").exists()
    assert (tmp_path / "b" / "models--org--repo" / "blobs" / "abc").exists()
    assert not (tmp_path / ("b" + storage_api.STAGING_SUFFIX)).exists()
    snap = tmp_path / "b" / "models--org--repo" / "snapshots" / "c0ffee" / "model.safetensors"
    assert snap.is_symlink(), "symlinks are copied as links, not dereferenced"


def test_move_tree_failed_copy_keeps_the_source_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_models(tmp_path / "a")
    cross_volume(monkeypatch)
    real_copytree = shutil.copytree

    def half_copy(src: Any, dst: Any, **kwargs: Any) -> Any:
        Path(dst).mkdir()
        (Path(dst) / "partial").write_text("x")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copytree", half_copy)
    with pytest.raises(OSError):
        move_tree(tmp_path / "a", tmp_path / "b")
    monkeypatch.setattr(shutil, "copytree", real_copytree)
    assert (tmp_path / "a" / "models--org--repo" / "blobs" / "abc").exists()
    assert not (tmp_path / "b").exists()
    assert not (tmp_path / ("b" + storage_api.STAGING_SUFFIX)).exists()


def test_move_tree_refuses_a_leftover_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_models(tmp_path / "a")
    (tmp_path / ("b" + storage_api.STAGING_SUFFIX)).mkdir()
    cross_volume(monkeypatch)
    with pytest.raises(OSError, match="interrupted move"):
        move_tree(tmp_path / "a", tmp_path / "b")
    assert (tmp_path / "a").exists()


# --- POST /storage/move ------------------------------------------------------------------


def test_move_models_directory(app: FastAPI, client: TestClient, tmp_path: Path) -> None:
    models = app.state.manager.settings.models_dir()
    seed_models(models)
    destination = tmp_path / "elsewhere" / "models"
    response = client.post(
        "/api/admin/storage/move", json={"target": "models", "path": str(destination)}
    )
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["job_id"])
    assert job["state"] == "done", job
    assert job["kind"] == "storage_move"
    assert (destination / "models--org--repo" / "blobs" / "abc").exists()
    assert not models.exists()
    assert storage_setting(client, "models_dir") == str(destination.resolve())
    assert client.get("/api/admin/storage").json()["models_dir"] == str(destination.resolve())
    assert any(j["job_id"] == job["job_id"] for j in client.get("/api/admin/jobs").json()["jobs"])


def test_a_storage_move_announces_the_settings_it_saved(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    """Every successful settings write sends `settings.changed`."""
    announced: list[dict[str, Any]] = []
    app.state.manager.events.listeners.append(
        lambda event, data: announced.append(data) if event == "settings.changed" else None
    )
    seed_models(app.state.manager.settings.models_dir())
    destination = tmp_path / "announced" / "models"
    response = client.post(
        "/api/admin/storage/move", json={"target": "models", "path": str(destination)}
    )
    assert response.status_code == 202, response.text
    assert wait_job(client, response.json()["job_id"])["state"] == "done"
    assert [change["key"] for change in announced[-1]["changed"]] == ["storage.models_dir"]


def test_move_cache_into_an_existing_empty_folder(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    cache = app.state.manager.settings.cache_dir()
    (cache / "ns").mkdir(parents=True, exist_ok=True)
    (cache / "ns" / "slot").write_bytes(b"kv")
    destination = tmp_path / "cache2"
    destination.mkdir()
    response = client.post(
        "/api/admin/storage/move", json={"target": "cache", "path": str(destination)}
    )
    assert wait_job(client, response.json()["job_id"])["state"] == "done"
    assert (destination / "ns" / "slot").read_bytes() == b"kv"
    assert storage_setting(client, "cache_dir") == str(destination.resolve())
    # The engine's TMPDIR follows the cache directory.
    assert client.get("/api/admin/storage").json()["tmp_dir"] == str(destination.resolve() / "tmp")


def test_move_without_files_just_points_the_setting(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    models = app.state.manager.settings.models_dir()
    seed_models(models)
    destination = tmp_path / "fresh"
    response = client.post(
        "/api/admin/storage/move",
        json={"target": "models", "path": str(destination), "move_files": False},
    )
    assert wait_job(client, response.json()["job_id"])["state"] == "done"
    assert destination.is_dir() and not any(destination.iterdir())
    assert (models / "models--org--repo").exists(), "the old files stay where they were"


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("relative/dir", 400, "invalid_storage_path"),
        ("{models}", 400, "invalid_storage_path"),
        ("{models}/inside", 400, "invalid_storage_path"),
        ("{models_parent}", 400, "invalid_storage_path"),
        ("{full}", 409, "destination_not_empty"),
        ("{file}", 409, "destination_not_empty"),
    ],
)
def test_move_refuses_bad_destinations(
    app: FastAPI, client: TestClient, tmp_path: Path, path: str, status: int, code: str
) -> None:
    models = app.state.manager.settings.models_dir()
    models.mkdir(parents=True, exist_ok=True)
    full = tmp_path / "full"
    full.mkdir()
    (full / "x").write_text("x")
    (tmp_path / "file").write_text("x")
    value = path.format(
        models=models, models_parent=models.parent, full=full, file=tmp_path / "file"
    )
    response = client.post("/api/admin/storage/move", json={"target": "models", "path": value})
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def test_move_refuses_while_the_engine_runs(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    app.state.manager.supervisor.state = "ready"
    response = client.post(
        "/api/admin/storage/move", json={"target": "models", "path": str(tmp_path / "m")}
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "engine_running"


def test_move_refuses_when_space_is_short(
    app: FastAPI, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_models(app.state.manager.settings.models_dir())
    real_stat = Path.stat
    models = app.state.manager.settings.models_dir().resolve()

    class OtherDevice:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        @property
        def st_dev(self) -> int:
            return -1

    def stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        result = real_stat(self, *args, **kwargs)
        return OtherDevice(result) if self == models else result

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(
        storage_api.shutil,  # type: ignore[attr-defined]
        "disk_usage",
        lambda _p: shutil._ntuple_diskusage(10, 10, 0),
    )
    response = client.post(
        "/api/admin/storage/move", json={"target": "models", "path": str(tmp_path / "m")}
    )
    assert response.status_code == 507 and response.json()["error"]["code"] == "disk_full"


def test_a_failed_move_changes_nothing(
    app: FastAPI, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = app.state.manager.settings.models_dir()
    seed_models(models)
    before = storage_setting(client, "models_dir")

    def broken(source: Path, destination: Path) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(storage_api, "move_tree", broken)
    destination = tmp_path / "target"
    destination.mkdir()
    response = client.post(
        "/api/admin/storage/move", json={"target": "models", "path": str(destination)}
    )
    job = wait_job(client, response.json()["job_id"])
    assert job["state"] == "failed" and "nothing was changed" in job["message"]
    assert storage_setting(client, "models_dir") == before
    assert (models / "models--org--repo" / "blobs" / "abc").exists()
    assert destination.is_dir(), "the user's empty folder is put back"


def test_a_settings_failure_moves_the_files_back(
    app: FastAPI, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = app.state.manager
    models = state.settings.models_dir()
    seed_models(models)

    def refuse(document: Any) -> Any:
        raise OSError("disk full while writing settings.json")

    monkeypatch.setattr(state.settings, "save", refuse)
    destination = tmp_path / "moved"
    response = client.post(
        "/api/admin/storage/move", json={"target": "models", "path": str(destination)}
    )
    job = wait_job(client, response.json()["job_id"])
    assert job["state"] == "failed" and "restored" in job["message"]
    assert (models / "models--org--repo" / "blobs" / "abc").exists()
    assert not destination.exists()


def test_engine_load_waits_for_a_storage_job(app: FastAPI, client: TestClient) -> None:
    state = app.state.manager

    async def forever(job: Any) -> None:
        import asyncio

        await asyncio.sleep(30)

    accepted = client.portal.call(lambda: _start(state, forever))  # type: ignore[union-attr]
    try:
        response = client.post("/api/admin/engine/load", json={"model": "org/repo"})
        # Not installed is checked first; use an installed name to reach the guard.
        state.installed_models = lambda: frozenset({"org/repo"})
        response = client.post("/api/admin/engine/load", json={"model": "org/repo"})
        assert response.status_code == 409 and response.json()["error"]["code"] == "storage_busy"
        again = client.post("/api/admin/storage/move", json={"target": "models", "path": "/tmp/x"})
        assert again.status_code == 409 and again.json()["error"]["code"] == "storage_busy"
    finally:
        job = state.jobs.get(accepted.job_id)
        client.portal.call(lambda: _cancel(job))  # type: ignore[union-attr]


async def _start(state: Any, body: Any) -> Any:
    return state.jobs.start("storage_move", body)


async def _cancel(job: Any) -> None:
    job.task.cancel()


def test_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/api/admin/jobs/nope").status_code == 404


# --- Imports -------------------------------------------------------------------------------


def hf_repo(hub: Path, repo: str, config: dict[str, Any]) -> Path:
    folder = hub / ("models--" + repo.replace("/", "--"))
    snap = folder / "snapshots" / "deadbeef"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text(json.dumps(config))
    (folder / "blobs").mkdir()
    (folder / "blobs" / "w").write_bytes(b"w" * 64)
    return folder


DENSE = {"text_config": {"hidden_size": 5120, "num_hidden_layers": 64}}
MOE = {"hidden_size": 2048, "num_hidden_layers": 40}
DRAFT = {"architectures": ["DFlash2DraftModel"]}
OTHER = {"hidden_size": 4096, "num_hidden_layers": 32}


@pytest.fixture
def hub(isolated_home: Path) -> Path:
    path = isolated_home / "hf-home" / "hub"
    hf_repo(path, "mlx-community/Qwen3.8-27B-4bit", DENSE)
    hf_repo(path, "unsloth/Qwen3.6-35B-A3B-GGUF", MOE)
    hf_repo(path, "incoai/Qwen3.8-27B-DFlash2", DRAFT)
    hf_repo(path, "someone/llama", OTHER)
    return path


def test_import_candidates_match_by_architecture(client: TestClient, hub: Path) -> None:
    body = client.get("/api/admin/storage/import-candidates").json()
    assert body["source_dir"] == str(hub)
    by_repo = {c["repo_id"]: c for c in body["candidates"]}
    assert set(by_repo) == {
        "mlx-community/Qwen3.8-27B-4bit",
        "unsloth/Qwen3.6-35B-A3B-GGUF",
        "incoai/Qwen3.8-27B-DFlash2",
    }
    assert by_repo["mlx-community/Qwen3.8-27B-4bit"]["family"] == "Qwen3.8-27B"
    assert by_repo["unsloth/Qwen3.6-35B-A3B-GGUF"]["family"] == "Qwen3.6-35B-A3B"
    assert by_repo["incoai/Qwen3.8-27B-DFlash2"]["kind"] == "draft"
    assert all(c["size_bytes"] > 0 for c in body["candidates"])


def test_import_moves_the_selected_repositories(
    app: FastAPI, client: TestClient, hub: Path
) -> None:
    models = app.state.manager.settings.models_dir()
    repos = ["mlx-community/Qwen3.8-27B-4bit", "incoai/Qwen3.8-27B-DFlash2"]
    response = client.post("/api/admin/storage/import", json={"repo_ids": repos})
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["job_id"])
    assert job["state"] == "done", job
    assert (models / "models--mlx-community--Qwen3.8-27B-4bit" / "blobs" / "w").exists()
    assert not (hub / "models--mlx-community--Qwen3.8-27B-4bit").exists()
    assert (hub / "models--unsloth--Qwen3.6-35B-A3B-GGUF").exists(), "unselected stays"


def test_import_refuses_unknown_and_conflicting_repositories(
    app: FastAPI, client: TestClient, hub: Path
) -> None:
    bad = client.post("/api/admin/storage/import", json={"repo_ids": ["someone/llama"]})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_import"
    empty = client.post("/api/admin/storage/import", json={"repo_ids": []})
    assert empty.status_code == 400
    models = app.state.manager.settings.models_dir()
    (models / "models--unsloth--Qwen3.6-35B-A3B-GGUF").mkdir(parents=True)
    conflict = client.post(
        "/api/admin/storage/import", json={"repo_ids": ["unsloth/Qwen3.6-35B-A3B-GGUF"]}
    )
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "import_conflict"


def test_a_failed_import_puts_everything_back(
    app: FastAPI, client: TestClient, hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = app.state.manager.settings.models_dir()
    real = storage_api.move_tree
    calls: list[str] = []

    def second_fails(source: Path, destination: Path) -> None:
        calls.append(source.name)
        if source.name == "models--unsloth--Qwen3.6-35B-A3B-GGUF":
            raise OSError(errno.ENOSPC, "No space left on device")
        real(source, destination)

    monkeypatch.setattr(storage_api, "move_tree", second_fails)
    repos = ["mlx-community/Qwen3.8-27B-4bit", "unsloth/Qwen3.6-35B-A3B-GGUF"]
    response = client.post("/api/admin/storage/import", json={"repo_ids": repos})
    job = wait_job(client, response.json()["job_id"])
    assert job["state"] == "failed" and "nothing was imported" in job["message"]
    assert (hub / "models--mlx-community--Qwen3.8-27B-4bit" / "blobs" / "w").exists()
    assert not (models / "models--mlx-community--Qwen3.8-27B-4bit").exists()


def test_default_hub_prefers_hf_hub_cache_unless_it_is_ours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ours = tmp_path / "ours"
    monkeypatch.setenv("HF_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    assert default_hf_hub(ours) == tmp_path / "home" / "hub"
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "custom"))
    assert default_hf_hub(ours) == tmp_path / "custom"
    monkeypatch.setenv("HF_HUB_CACHE", str(ours))
    assert default_hf_hub(ours) == tmp_path / "home" / "hub"

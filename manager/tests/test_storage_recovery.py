"""A storage move cut short by a killed manager is settled at the next start:
each test lays out the files and the move record as they would be at one crash
point, runs the startup recovery, and checks the setting and the files."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.app import AppConfig, create_app
from splash_gui.paths import Paths
from splash_gui.schemas import StorageMoveRequest
from splash_gui.secrets import SecretStore
from splash_gui.settings.store import SettingsStore
from splash_gui.storage import api as storage_api
from splash_gui.storage.recovery import MoveRecord, record_path, recover, staging_for

from .test_storage import held_move_tree, seed_models, wait_job

BLOB = Path("models--org--repo") / "blobs" / "abc"


@pytest.fixture
def store(paths: Paths) -> SettingsStore:
    settings = SettingsStore(paths)
    settings.load()
    return settings


@pytest.fixture
def source(paths: Paths) -> Path:
    seed_models(paths.models_dir)
    return paths.models_dir.resolve()


@pytest.fixture
def destination(tmp_path: Path) -> Path:
    return (tmp_path / "external" / "models").resolve()


def write_record(paths: Paths, source: Path, destination: Path, **fields: Any) -> None:
    MoveRecord(target="models", source=str(source), destination=str(destination), **fields).save(
        paths
    )


def point_at(store: SettingsStore, folder: Path) -> None:
    document = store.current.model_dump(mode="json", by_alias=True)
    document["global"]["storage"]["models_dir"] = str(folder)
    assert store.save(document)[0].ok


def has_models(folder: Path) -> bool:
    return (folder / BLOB).is_file()


def test_no_record_changes_nothing(paths: Paths, store: SettingsStore, source: Path) -> None:
    assert recover(paths, store) == []
    assert store.models_dir() == paths.models_dir


def test_killed_before_any_file_moved_keeps_the_source(
    paths: Paths, store: SettingsStore, source: Path, destination: Path
) -> None:
    destination.mkdir(parents=True)
    write_record(paths, source, destination, destination_existed=True)
    assert recover(paths, store) == []
    assert store.models_dir().resolve() == source
    assert has_models(source)
    assert destination.is_dir() and not any(destination.iterdir())
    assert not record_path(paths).exists()


def test_killed_after_emptying_the_destination_puts_the_folder_back(
    paths: Paths, store: SettingsStore, source: Path, destination: Path
) -> None:
    destination.parent.mkdir(parents=True)
    write_record(paths, source, destination, destination_existed=True)
    recover(paths, store)
    assert store.models_dir().resolve() == source
    assert destination.is_dir(), "the user's empty folder is back"


def test_a_same_volume_rename_that_landed_moves_the_setting(
    paths: Paths, store: SettingsStore, source: Path, destination: Path
) -> None:
    destination.parent.mkdir(parents=True)
    source.rename(destination)
    write_record(paths, source, destination)
    assert recover(paths, store) == []
    assert store.models_dir() == destination
    assert has_models(destination)
    assert not record_path(paths).exists()
    # The saved setting survives a restart.
    assert SettingsStore(paths).load().global_.storage.models_dir == str(destination)


def test_a_partial_cross_volume_copy_is_removed_and_the_source_kept(
    paths: Paths, store: SettingsStore, source: Path, destination: Path
) -> None:
    staging = staging_for(destination)
    (staging / "models--org--repo" / "blobs").mkdir(parents=True)
    (staging / "models--org--repo" / "blobs" / "abc").write_bytes(b"wei")
    write_record(paths, source, destination)
    assert recover(paths, store) == []
    assert not staging.exists()
    assert has_models(source)
    assert store.models_dir().resolve() == source


def test_a_staging_folder_from_an_earlier_move_is_left_alone(
    paths: Paths, store: SettingsStore, source: Path, destination: Path
) -> None:
    staging = staging_for(destination)
    staging.mkdir(parents=True)
    (staging / "keep").write_text("not ours")
    write_record(paths, source, destination, staging_existed=True)
    recover(paths, store)
    assert (staging / "keep").read_text() == "not ours"
    assert store.models_dir().resolve() == source


def test_a_landed_copy_whose_source_was_not_yet_deleted_keeps_both(
    paths: Paths, store: SettingsStore, source: Path, destination: Path
) -> None:
    seed_models(destination)
    write_record(paths, source, destination)
    warnings = recover(paths, store)
    assert store.models_dir() == destination
    assert has_models(source), "the old folder is kept for the user to remove"
    assert len(warnings) == 1 and str(source) in warnings[0]


def test_killed_while_saving_the_setting(
    paths: Paths, store: SettingsStore, source: Path, destination: Path
) -> None:
    destination.parent.mkdir(parents=True)
    source.rename(destination)
    write_record(paths, source, destination, phase="moved")
    recover(paths, store)
    assert store.models_dir() == destination


def test_killed_after_saving_the_setting_leaves_it(
    paths: Paths, store: SettingsStore, source: Path, destination: Path
) -> None:
    destination.parent.mkdir(parents=True)
    source.rename(destination)
    point_at(store, destination)
    write_record(paths, source, destination, phase="moved")
    assert recover(paths, store) == []
    assert store.models_dir() == destination
    assert not record_path(paths).exists()


def test_killed_while_copying_back_keeps_the_destination(
    paths: Paths, store: SettingsStore, source: Path, destination: Path
) -> None:
    destination.parent.mkdir(parents=True)
    source.rename(destination)
    back = staging_for(source)
    back.mkdir()
    (back / "partial").write_text("x")
    write_record(paths, source, destination, phase="restoring")
    recover(paths, store)
    assert not back.exists()
    assert has_models(destination)
    assert store.models_dir() == destination


def test_a_finished_move_back_keeps_the_source(
    paths: Paths, store: SettingsStore, source: Path, destination: Path
) -> None:
    destination.parent.mkdir(parents=True)
    write_record(paths, source, destination, phase="restoring")
    recover(paths, store)
    assert store.models_dir().resolve() == source
    assert has_models(source)


def test_files_in_neither_place_change_nothing_and_warn(
    paths: Paths, store: SettingsStore, destination: Path
) -> None:
    missing = paths.base / "gone"
    destination.parent.mkdir(parents=True)
    write_record(paths, missing, destination)
    warnings = recover(paths, store)
    assert store.models_dir() == paths.models_dir
    assert any("unchanged" in w for w in warnings)
    assert not record_path(paths).exists()


def test_an_interrupted_copy_without_its_source_deletes_nothing(
    paths: Paths, store: SettingsStore, destination: Path
) -> None:
    staging = staging_for(destination)
    staging.mkdir(parents=True)
    (staging / "partial").write_text("x")
    write_record(paths, paths.base / "gone", destination)
    warnings = recover(paths, store)
    assert (staging / "partial").exists()
    assert store.models_dir() == paths.models_dir
    assert warnings


def test_an_unmounted_source_drive_keeps_the_setting_and_the_record(
    paths: Paths, store: SettingsStore, tmp_path: Path
) -> None:
    # The files live on a drive that isn't mounted at this start; the manager has
    # just recreated its empty default folder, the move's destination.
    external = (tmp_path / "Volumes" / "X" / "models").resolve()
    point_at(store, external)
    paths.models_dir.mkdir(parents=True, exist_ok=True)
    write_record(paths, external, paths.models_dir.resolve(), destination_existed=True)
    warnings = recover(paths, store)
    assert store.models_dir() == external, "never points at the empty default folder"
    assert any("can't be reached" in w for w in warnings)
    assert record_path(paths).exists(), "kept for a start where the drive is mounted"

    # Once the drive is back the move settles: the files never left it.
    seed_models(external)
    assert recover(paths, store) == []
    assert store.models_dir() == external
    assert not record_path(paths).exists()


def test_an_unmounted_destination_drive_keeps_a_partly_deleted_source(
    paths: Paths, store: SettingsStore, source: Path, tmp_path: Path
) -> None:
    # Killed while deleting the source after the copy landed on a drive that is
    # unplugged at this start: the source may be missing files.
    external = (tmp_path / "Volumes" / "X" / "models").resolve()
    write_record(paths, source, external, phase="moving")
    warnings = recover(paths, store)
    assert any("can't be reached" in w for w in warnings)
    assert has_models(source)
    assert record_path(paths).exists()

    seed_models(external)
    recover(paths, store)
    assert store.models_dir() == external
    assert not record_path(paths).exists()


def test_an_empty_recreated_source_does_not_hide_a_landed_move(
    paths: Paths, store: SettingsStore, source: Path, destination: Path
) -> None:
    destination.parent.mkdir(parents=True)
    source.rename(destination)
    source.mkdir()  # the startup's default folder, recreated empty
    write_record(paths, source, destination)
    assert recover(paths, store) == [], "an empty folder isn't a leftover copy"
    assert store.models_dir() == destination


@pytest.mark.parametrize(
    "text", ["{not json", "[]", '{"version": 1, "target": "models"}', '{"version": 9}']
)
def test_an_unreadable_record_is_dropped_without_failing(
    paths: Paths, store: SettingsStore, source: Path, text: str
) -> None:
    record_path(paths).write_text(text)
    warnings = recover(paths, store)
    assert warnings and "unreadable" in warnings[0]
    assert not record_path(paths).exists()
    assert store.models_dir() == paths.models_dir
    assert has_models(source)


def test_startup_settles_the_move_before_serving(
    paths: Paths, secrets: SecretStore, web_dist: Path, source: Path, destination: Path
) -> None:
    destination.parent.mkdir(parents=True)
    source.rename(destination)
    write_record(paths, source, destination)
    app = create_app(AppConfig(paths=paths, web_dist=web_dist, secrets=secrets))
    assert app.state.manager.settings.models_dir() == destination
    assert not record_path(paths).exists()


def test_startup_survives_a_corrupt_record(
    paths: Paths, secrets: SecretStore, web_dist: Path
) -> None:
    record_path(paths).write_text("\x00garbage")
    app = create_app(AppConfig(paths=paths, web_dist=web_dist, secrets=secrets))
    token = app.state.manager.auth.cli_token()
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, client=("127.0.0.1", 50000), headers=headers) as client:
        body = client.get("/api/admin/settings").json()
    assert any("interrupted" in w for w in body["load_warnings"])


# --- the record during a real move --------------------------------------------------------


async def test_the_record_exists_while_files_move_and_is_removed_after(
    app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = app.state.manager
    seed_models(state.settings.models_dir())
    destination = tmp_path / "moved-models"
    started, release = held_move_tree(monkeypatch)
    accepted = await storage_api.move(
        state, StorageMoveRequest(target="models", path=str(destination), move_files=True)
    )
    assert await asyncio.to_thread(started.wait, 5)
    record = json.loads(record_path(state.paths).read_text())
    assert record["phase"] == "moving"
    assert record["destination"] == str(destination.resolve())
    release.set()
    job = state.jobs.get(accepted.job_id)
    assert job is not None and job.task is not None
    await asyncio.wait_for(job.task, 10)
    assert job.state == "done"
    assert not record_path(state.paths).exists()


def test_a_move_restored_after_a_settings_failure_removes_the_record(
    app: FastAPI, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = app.state.manager
    seed_models(state.settings.models_dir())

    def refuse(document: Any) -> Any:
        raise OSError("disk full while writing settings.json")

    monkeypatch.setattr(state.settings, "save", refuse)
    response = client.post(
        "/api/admin/storage/move", json={"target": "models", "path": str(tmp_path / "moved")}
    )
    assert wait_job(client, response.json()["job_id"])["state"] == "failed"
    assert not record_path(state.paths).exists()


def test_a_failed_move_removes_the_record(
    app: FastAPI, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = app.state.manager
    seed_models(state.settings.models_dir())

    def broken(source: Path, destination: Path) -> None:
        raise OSError("copy failed")

    monkeypatch.setattr(storage_api, "move_tree", broken)
    response = client.post(
        "/api/admin/storage/move", json={"target": "models", "path": str(tmp_path / "moved")}
    )
    assert wait_job(client, response.json()["job_id"])["state"] == "failed"
    assert not record_path(state.paths).exists()

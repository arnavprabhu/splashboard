"""Storage locations, checked moves, and supported-cache imports (SPEC §5)."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends

from ..errors import ApiError
from ..jobs import Job, JobFailed
from ..models.layout import directory_size, repo_of_folder
from ..paths import splash_data_dir
from ..schemas import (
    ImportCandidate,
    ImportCandidates,
    ImportRequest,
    JobAccepted,
    ModelsChangedEvent,
    StorageInfo,
    StorageMoveRequest,
)
from ..state import ManagerState, get_state

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


@router.get("/storage", response_model=StorageInfo)
def storage(state: State) -> StorageInfo:
    models = state.settings.models_dir()
    models.mkdir(parents=True, exist_ok=True)
    return StorageInfo(
        models_dir=str(models),
        cache_dir=str(state.settings.cache_dir()),
        tmp_dir=str(state.settings.tmp_dir()),
        splash_data_dir=str(splash_data_dir()),
        models_bytes=directory_size(models),
        cache_bytes=directory_size(state.settings.cache_dir()),
        splash_data_bytes=directory_size(splash_data_dir()),
        free_bytes=shutil.disk_usage(models).free,
    )


def ensure_idle(state: ManagerState) -> None:
    if state.supervisor.state not in ("stopped", "failed"):
        raise ApiError(409, "Stop the engine before moving storage", "engine_running")
    if state.downloads and any(
        i.state in ("queued", "running", "verifying") for i in state.downloads.items.values()
    ):
        raise ApiError(409, "Pause downloads before moving storage", "downloads_running")
    if state.jobs.running("storage_move") or state.jobs.running("import"):
        raise ApiError(409, "Another storage operation is running", "storage_busy")


def move_tree(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.rename(destination)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        staging = destination.with_name(destination.name + ".splash-moving")
        if staging.exists():
            raise OSError("An interrupted move exists at " + str(staging)) from error
        shutil.copytree(source, staging, symlinks=True)
        staging.rename(destination)
        shutil.rmtree(source)


@router.post("/storage/move", response_model=JobAccepted, status_code=202)
async def move(state: State, body: StorageMoveRequest) -> JobAccepted:
    ensure_idle(state)
    source = state.settings.models_dir() if body.target == "models" else state.settings.cache_dir()
    destination = Path(body.path).expanduser().resolve()
    source = source.resolve()
    if (
        not Path(body.path).expanduser().is_absolute()
        or destination == source
        or destination.is_relative_to(source)
        or source.is_relative_to(destination)
    ):
        raise ApiError(400, "Choose a separate absolute directory", "invalid_storage_path")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ApiError(409, "The destination must be an empty directory", "destination_not_empty")
    probe = destination.parent
    while not probe.exists():
        probe = probe.parent
    if (
        source.exists()
        and source.stat().st_dev != probe.stat().st_dev
        and shutil.disk_usage(probe).free < directory_size(source)
    ):
        raise ApiError(507, "Not enough space at the destination", "disk_full")

    async def run(job: Job) -> None:
        job.update(message="Moving files…")
        if destination.exists():
            destination.rmdir()
        if body.move_files and source.exists():
            await asyncio.to_thread(move_tree, source, destination)
        else:
            destination.mkdir(parents=True, mode=0o700)
        document = state.settings.current.model_dump(mode="json", by_alias=True)
        document["global"]["storage"][body.target + "_dir"] = str(destination)
        result, changes = state.settings.save(document)
        if not result.ok:
            if body.move_files and not source.exists():
                await asyncio.to_thread(move_tree, destination, source)
            raise JobFailed("Settings could not be saved; storage was restored")
        for listener in state.settings_listeners:
            listener(changes, False)
        state.events.publish("models.changed", ModelsChangedEvent(reason="moved"))

    return state.jobs.start("storage_move", run)


@router.get("/storage/import-candidates", response_model=ImportCandidates)
def import_candidates(state: State) -> ImportCandidates:
    source = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))) / "hub"
    candidates = []
    for folder in sorted(source.glob("models--*")):
        repo = repo_of_folder(folder.name)
        if not repo or folder.is_symlink():
            continue
        # Only architecture metadata may qualify an import; names alone are insufficient.
        for config in (folder / "snapshots").glob("*/config.json"):
            try:
                data = json.loads(config.read_text())
            except (OSError, ValueError):
                continue
            text = data.get("text_config", data)
            family = (
                "Qwen3.8-27B"
                if text.get("hidden_size") == 5120 and text.get("num_hidden_layers") == 64
                else "Qwen3.6-35B-A3B"
                if text.get("hidden_size") == 2048 and text.get("num_hidden_layers") == 40
                else None
            )
            draft = "DFlash2DraftModel" in data.get("architectures", [])
            if family or draft:
                candidates.append(
                    ImportCandidate.model_validate(
                        {
                            "repo_id": repo,
                            "path": str(folder),
                            "size_bytes": directory_size(folder),
                            "kind": "draft" if draft else "model",
                            "family": family,
                        }
                    )
                )
                break
    return ImportCandidates(source_dir=str(source), candidates=candidates)


@router.post("/storage/import", response_model=JobAccepted, status_code=202)
async def import_models(state: State, body: ImportRequest) -> JobAccepted:
    ensure_idle(state)
    available = {c.repo_id: c for c in import_candidates(state).candidates}
    if not body.repo_ids or any(repo not in available for repo in body.repo_ids):
        raise ApiError(400, "Select repositories from the import candidates", "invalid_import")
    selected = [available[repo] for repo in dict.fromkeys(body.repo_ids)]
    target = state.settings.models_dir()
    if any((target / Path(c.path).name).exists() for c in selected):
        raise ApiError(
            409, "A selected repository already exists in the models directory", "import_conflict"
        )

    async def run(job: Job) -> None:
        target.mkdir(parents=True, exist_ok=True)
        for index, candidate in enumerate(selected):
            await asyncio.to_thread(
                move_tree, Path(candidate.path), target / Path(candidate.path).name
            )
            job.update(progress=(index + 1) / len(selected), message=candidate.repo_id)
        state.events.publish("models.changed", ModelsChangedEvent(reason="imported"))

    return state.jobs.start("import", run)

"""Storage locations, checked moves, and supported-cache imports."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
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
from ..settings.api import settings_changed
from ..state import ManagerState, get_state

log = logging.getLogger(__name__)
router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


@router.get("/storage", response_model=StorageInfo)
def storage(state: State) -> StorageInfo:
    models = state.settings.models_dir()
    models.mkdir(parents=True, exist_ok=True)
    hub = user_hf_hub()
    return StorageInfo(
        models_shared_with_hf_cache=shares_hf_cache(models, hub),
        hf_cache_path=str(hub),
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


STAGING_SUFFIX = ".splash-moving"


def move_tree(source: Path, destination: Path) -> None:
    """Move a directory tree: an atomic rename on the same volume, otherwise copy
    to a staging directory beside the destination, rename it into place, then
    delete the source.

    A failure before the staging rename leaves the source untouched and removes
    the partial copy, so nothing is lost and the move can simply be retried.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.rename(destination)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    staging = destination.with_name(destination.name + STAGING_SUFFIX)
    if staging.exists():
        raise OSError(f"An interrupted move exists at {staging}; remove it and retry")
    try:
        shutil.copytree(source, staging, symlinks=True)
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # The copy is complete and in place; only now is the source removed.
    shutil.rmtree(source)


def user_hf_hub() -> Path:
    """The user's own Hugging Face hub cache, as `hf download` would use it:
    `HF_HUB_CACHE`, else `HF_HOME/hub`, else ~/.cache/huggingface/hub."""
    explicit = os.environ.get("HF_HUB_CACHE")
    if explicit:
        return Path(explicit).expanduser()
    home = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface")))
    return home.expanduser() / "hub"


def shares_hf_cache(models: Path, hub: Path) -> bool:
    """The models directory is (inside, or contains) the user's own HF cache,
    so deleting a model can remove files they downloaded themselves."""
    try:
        a, b = models.resolve(), hub.resolve()
    except OSError:
        return False
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def default_hf_hub(models_dir: Path) -> Path:
    """Hugging Face's default hub cache (`HF_HUB_CACHE`, else `HF_HOME/hub`), the
    place earlier downloads live, unless that is our own models directory."""
    explicit = os.environ.get("HF_HUB_CACHE")
    if explicit and Path(explicit).expanduser().resolve() != models_dir.resolve():
        return Path(explicit).expanduser()
    home = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface")))
    return home.expanduser() / "hub"


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
        created = not destination.exists()
        moved = False
        try:
            if body.move_files and source.exists():
                if destination.exists():
                    destination.rmdir()  # checked empty above
                await asyncio.to_thread(move_tree, source, destination)
                moved = True
            else:
                destination.mkdir(parents=True, mode=0o700, exist_ok=True)
        except OSError as error:
            if not destination.exists() and not created:
                destination.mkdir(parents=True, exist_ok=True)  # put the empty folder back
            raise JobFailed(f"The move failed and nothing was changed: {error}") from None
        job.update(progress=0.9, message="Saving settings…")
        old = state.settings.current
        document = old.model_dump(mode="json", by_alias=True)
        document["global"]["storage"][body.target + "_dir"] = str(destination)
        try:
            result, changes = state.settings.save(document)
            ok = result.ok
        except Exception:
            log.exception("saving the storage setting failed")
            ok, changes = False, []
        if not ok:
            if moved:
                await asyncio.to_thread(move_tree, destination, source)
            elif created:
                shutil.rmtree(destination, ignore_errors=True)
            raise JobFailed("Settings could not be saved; storage was restored")
        for listener in state.settings_listeners:
            listener(changes, False)
        if result.document is not None:
            state.events.publish(
                "settings.changed", settings_changed(state, old, result.document, changes)
            )
        job.line(f"{body.target} directory is now {destination}")
        state.events.publish("models.changed", ModelsChangedEvent(reason="moved"))

    return state.jobs.start("storage_move", run)


@router.get("/storage/import-candidates", response_model=ImportCandidates)
def import_candidates(state: State) -> ImportCandidates:
    source = default_hf_hub(state.settings.models_dir())
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
        done: list[tuple[Path, Path]] = []
        for index, candidate in enumerate(selected):
            origin = Path(candidate.path)
            moved_to = target / origin.name
            try:
                await asyncio.to_thread(move_tree, origin, moved_to)
            except OSError as error:
                # All or nothing: put back what this import already moved.
                for back_from, back_to in reversed(done):
                    try:
                        await asyncio.to_thread(move_tree, back_from, back_to)
                    except OSError:
                        log.exception("could not move %s back to %s", back_from, back_to)
                raise JobFailed(
                    f"Importing {candidate.repo_id} failed ({error}); nothing was imported"
                ) from None
            done.append((moved_to, origin))
            job.line(f"imported {candidate.repo_id}")
            job.update(progress=(index + 1) / len(selected), message=candidate.repo_id)
        state.events.publish("models.changed", ModelsChangedEvent(reason="imported"))

    return state.jobs.start("import", run)

"""Data sizes and explicit clearing of user-selected categories (SPEC §10.9)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends

from ..errors import ApiError, error_responses
from ..logs.api import clear_logs, delete_trace, traces
from ..models.layout import directory_size
from ..schemas import DataClearRequest, DataClearResult, DataSize, DataSizes
from ..state import ManagerState, get_state

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


def _cache_bytes(cache: Path) -> int:
    """The persistent KV cache: everything under the cache directory except the
    session-only `tmp/` (SPEC §5), symlinks counted as links."""
    if not cache.is_dir():
        return 0
    total = 0
    for path in cache.iterdir():
        if path.name == "tmp":
            continue
        try:
            total += (
                directory_size(path)
                if path.is_dir() and not path.is_symlink()
                else path.lstat().st_size
            )
        except OSError:
            continue
    return total


@router.get("/data/sizes", response_model=DataSizes)
def sizes(state: State) -> DataSizes:
    trace_list = traces(state).traces
    return DataSizes(
        targets=[
            DataSize(
                target="chats",
                label="Chat history",
                bytes=directory_size(state.paths.chats_dir),
                items=sum(1 for _ in state.paths.chats_dir.glob("*.json"))
                if state.paths.chats_dir.is_dir()
                else 0,
            ),
            DataSize(
                target="usage",
                label="Usage history",
                bytes=state.usage.size_bytes(),
                items=state.usage.request_count(),
            ),
            DataSize(target="logs", label="Logs", bytes=directory_size(state.paths.logs_dir)),
            DataSize(
                target="traces",
                label="Crash traces",
                bytes=sum(t.size_bytes for t in trace_list),
                items=len(trace_list),
            ),
            DataSize(
                target="kv_cache",
                label="Persistent KV cache",
                bytes=_cache_bytes(state.settings.cache_dir()),
            ),
            DataSize(
                target="responses",
                label="Stored responses",
                bytes=None,
                note="Cleared by restarting the engine",
            ),
            DataSize(
                target="models", label="Models", bytes=directory_size(state.settings.models_dir())
            ),
        ]
    )


@router.post("/data/clear", response_model=DataClearResult, responses=error_responses(409))
async def clear(state: State, body: DataClearRequest) -> DataClearResult:
    before = (
        next(t.bytes or 0 for t in sizes(state).targets if t.target == body.target)
        if body.target != "models"
        else 0
    )
    stopped = restarted = False
    if body.target == "chats":
        state.chats.delete_all()
    elif body.target == "usage":
        state.usage.delete_requests()
    elif body.target == "logs":
        clear_logs(state)
    elif body.target == "traces":
        for trace in traces(state).traces:
            delete_trace(state, trace.name)
    elif body.target == "kv_cache":
        stopped = state.active_model() is not None
        await state.supervisor.stop(reason="clear_cache")
        cache = state.settings.cache_dir()
        for path in cache.iterdir() if cache.exists() else ():
            if path.name == "tmp":
                continue
            if path.is_dir() and not path.is_symlink():
                await asyncio.to_thread(shutil.rmtree, path)
            else:
                path.unlink()
    elif body.target == "responses":
        if state.active_model():
            await state.supervisor.restart(force=True)
            restarted = True
    elif body.target == "models":
        # Deleting every model is done in the Models manager with everything
        # selected (SPEC §10.9), where reference counting and the active-model
        # confirmation apply per model (docs/api.md §12).
        raise ApiError(
            409,
            "Delete models from the Models manager (select all, then Delete)",
            "use_models_manager",
        )
    after = next(t.bytes or 0 for t in sizes(state).targets if t.target == body.target)
    return DataClearResult(
        target=body.target,
        freed_bytes=max(0, before - after),
        engine_stopped=stopped,
        engine_restarted=restarted,
    )

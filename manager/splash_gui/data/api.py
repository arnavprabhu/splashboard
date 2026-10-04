"""Data sizes and explicit clearing of user-selected categories (SPEC §10.9)."""

from __future__ import annotations

import asyncio
import shutil
from typing import Annotated

from fastapi import APIRouter, Depends

from ..logs.api import clear_logs, delete_trace, traces
from ..models.layout import directory_size
from ..schemas import DataClearRequest, DataClearResult, DataSize, DataSizes
from ..state import ManagerState, get_state

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


@router.get("/data/sizes", response_model=DataSizes)
def sizes(state: State) -> DataSizes:
    cache = state.settings.cache_dir()
    return DataSizes(
        targets=[
            DataSize(
                target="chats", label="Chat history", bytes=directory_size(state.paths.chats_dir)
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
                bytes=sum(t.size_bytes for t in traces(state).traces),
            ),
            DataSize(
                target="kv_cache",
                label="Persistent KV cache",
                bytes=sum(
                    directory_size(p) if p.is_dir() else p.stat().st_size
                    for p in cache.iterdir()
                    if p.name != "tmp"
                )
                if cache.exists()
                else 0,
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


@router.post("/data/clear", response_model=DataClearResult)
async def clear(state: State, body: DataClearRequest) -> DataClearResult:
    before = next(t.bytes or 0 for t in sizes(state).targets if t.target == body.target)
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
        for model in list(state.models.inventory().models):
            result = await state.models.delete(model.id, confirm_active=True)
            stopped |= result.engine_stopped
    after = next(t.bytes or 0 for t in sizes(state).targets if t.target == body.target)
    return DataClearResult(
        target=body.target,
        freed_bytes=max(0, before - after),
        engine_stopped=stopped,
        engine_restarted=restarted,
    )

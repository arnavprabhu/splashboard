"""Data sizes and explicit clearing of user-selected categories."""

from __future__ import annotations

import asyncio
import fcntl
import os
import re
import shutil
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends

from ..engine.status import integer
from ..errors import ApiError, error_responses
from ..logs.api import clear_logs, delete_trace, traces
from ..models.layout import directory_size
from ..schemas import DataClearRequest, DataClearResult, DataSize, DataSizes
from ..state import ManagerState, get_state

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]
NAMESPACE = re.compile(r"[0-9a-f]{1,64}")


def _namespaces(cache: Path) -> list[Path]:
    """Splash's persistent-cache namespaces under the cache root: directories
    named in lowercase hex, at most 64 characters (`runtime/engine/
    CacheDirectory.cpp` `plainName`; RuntimeResources.mm names them with 32).
    Nothing else under a user-chosen `--cache-dir` is Splash's, so nothing else
    is counted or cleared; `tmp/` never matches."""
    if not cache.is_dir():
        return []
    return [
        path
        for path in sorted(cache.iterdir())
        if NAMESPACE.fullmatch(path.name) and path.is_dir() and not path.is_symlink()
    ]


def _namespace_held(namespace: Path) -> bool:
    """An engine (ours or a `splash serve` in a terminal) holds the namespace's
    `lock` file (CacheDirectory.cpp `kLock`, flock LOCK_EX)."""
    try:
        fd = os.open(namespace / "lock", os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return False


def _cache_bytes(cache: Path) -> int:
    """The persistent KV cache: Splash's namespaces under the cache directory."""
    total = 0
    for namespace in _namespaces(cache):
        try:
            total += directory_size(namespace)
        except OSError:
            continue
    return total


def _stored_responses(state: ManagerState) -> tuple[int | None, int | None]:
    """(bytes, entries) of the stored responses: the engine's `response_store`.

    The store lives in the engine process and goes with it, so it is read from the engine's
    last `/status` (`splash/server/frontend.py` `ResponseStore.stats`). With no engine running
    it is empty. An engine that is starting but not polled yet is unknown (None), which the UI
    shows as a dash."""
    supervisor = state.supervisor
    status = supervisor.status
    if status is None:
        return (None, None) if supervisor.accepting else (0, 0)
    return (
        integer(status, "response_store.bytes"),
        integer(status, "response_store.entries"),
    )


def clear_kv_cache(cache: Path) -> list[str]:
    """Delete every namespace no engine holds; returns the ones left in use."""
    held = []
    for namespace in _namespaces(cache):
        if _namespace_held(namespace):
            held.append(namespace.name)
            continue
        shutil.rmtree(namespace)
    return held


@router.get("/data/sizes", response_model=DataSizes)
def sizes(state: State) -> DataSizes:
    trace_list = traces(state).traces
    responses_bytes, responses_items = _stored_responses(state)
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
                bytes=responses_bytes,
                items=responses_items,
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
        # No auto-load may start the engine on the cache while it is being cleared.
        async with state.supervisor.hold("cache_clear"):
            await state.supervisor.stop(reason="clear_cache")
            await asyncio.to_thread(clear_kv_cache, state.settings.cache_dir())
    elif body.target == "responses":
        if state.active_model():
            await state.supervisor.restart(force=True)
            restarted = True
    elif body.target == "models":
        # Deleting every model is done in the Models manager with everything
        # selected, where reference counting and the active-model
        # confirmation apply per model.
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

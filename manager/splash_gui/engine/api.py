"""Engine routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from ..errors import ApiError, error_responses
from ..proxy.pipeline import ProxyError
from ..schemas import EngineView, LoadRequest, RestartRequest
from ..state import ManagerState, get_state

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


def get_engine(state: ManagerState) -> EngineView:
    return state.supervisor.view()  # type: ignore[no-any-return]


def load_target(state: ManagerState, model: str) -> str:
    """`ID:profile` loads ID (profiles are per request)."""
    installed = state.installed_models()
    if installed is None or model in installed:
        return model
    base, sep, _ = model.rpartition(":")
    if sep and base in installed:
        return base
    return model


@router.get("/engine", response_model=EngineView)
def engine_view(state: State) -> EngineView:
    return get_engine(state)


@router.post(
    "/engine/load",
    response_model=EngineView,
    status_code=202,
    responses=error_responses(400, 404, 409, 422, 503),
)
async def load(state: State, body: LoadRequest) -> EngineView:
    sup = state.supervisor
    view: EngineView = await sup.load(load_target(state, body.model), force=body.force)
    if body.wait:
        timeout = body.timeout or state.settings.current.global_.routing.load_timeout
        await sup.wait_ready(timeout)
        view = sup.view()
    return view


@router.post("/engine/stop", response_model=EngineView)
async def stop(state: State) -> EngineView:
    return await state.supervisor.stop(reason="stop")  # type: ignore[no-any-return]


@router.post(
    "/engine/restart",
    response_model=EngineView,
    status_code=202,
    responses=error_responses(409),
)
async def restart(
    state: State, force: bool = False, body: RestartRequest | None = None
) -> EngineView:
    """`force` (query or body) restarts with requests in flight or during an install."""
    forced = force or (body is not None and body.force)
    return await state.supervisor.restart(force=forced)  # type: ignore[no-any-return]


class MemoryReleased(BaseModel):
    released: bool


ENGINE_BUSY = "The engine is busy; try again when requests finish."
NO_MODEL_LOADED = "No model is loaded."


@router.post(
    "/engine/release-memory",
    response_model=MemoryReleased,
    responses=error_responses(409, 502, 503),
)
async def release_memory(state: State) -> MemoryReleased:
    """Release memory now: the engine gives its weights' memory back at once, as
    after its idle release (with the idle release off too), and the next request
    restores them.

    - 200 `{"released": true}`: the weights are released (already released counts).
    - 409 `engine_busy`: the engine is serving a request, restoring its weights or
      loading; try again when requests finish.
    - 409 `engine_not_loaded`: no model is loaded (also while the engine stops).
    - 502: any other engine refusal (its own code and message).
    - 503: the engine is shutting down, recovering, failed or did not answer (its own
      code and message, or `engine_unavailable`).
    """
    sup = state.supervisor
    if not sup.accepting:
        if sup.state == "starting":
            raise ApiError(409, ENGINE_BUSY, "engine_busy")
        raise ApiError(409, NO_MODEL_LOADED, "engine_not_loaded")
    try:
        upstream = await state.proxy.release_engine_memory()
    except ProxyError as error:
        raise ApiError(error.status, error.message, error.code, headers=error.headers) from None
    if upstream.status_code == 200:
        return MemoryReleased(released=True)
    try:
        failure = upstream.json()["error"]
        code, message = failure.get("code"), str(failure.get("message") or "")
    except (ValueError, KeyError, TypeError, AttributeError):
        code, message = None, ""
    if code == "engine_busy":
        # 409 while it serves or restores; 503 when it did not answer within 5 s.
        raise ApiError(409, ENGINE_BUSY, "engine_busy")
    # A 5xx (500 engine_failed, 503 engine_recovering or server_shutdown) means the engine
    # can't take it now: 503. Anything else is a refusal of its own: 502.
    status = 503 if upstream.status_code >= 500 else 502
    raise ApiError(
        status,
        message or f"The engine answered {upstream.status_code}",
        code or "engine_error",
    )


@router.get("/engine/status", response_model=dict[str, Any], responses=error_responses(503))
async def raw_status(state: State) -> dict[str, Any]:
    sup = state.supervisor
    status = await sup.poll_now() if sup.accepting else None
    if status is None:
        raise ApiError(
            503, "The engine is not running", "engine_unavailable", headers={"Retry-After": "5"}
        )
    return status  # type: ignore[no-any-return]


_RAW = error_responses(503)


@router.post("/engine/raw/{path:path}", responses=_RAW)
async def raw_post(state: State, request: Request, path: str) -> Response:
    """Playground "raw to engine": forwards to the engine with the internal key, no injection."""
    return await state.proxy.raw(request, "POST", path)  # type: ignore[no-any-return]


@router.get("/engine/raw/{path:path}", responses=_RAW)
async def raw_get(state: State, request: Request, path: str) -> Response:
    """Raw GET (e.g. `v1/responses/{id}`, `status`, `metrics`), as `raw_post`."""
    return await state.proxy.raw(request, "GET", path)  # type: ignore[no-any-return]


@router.delete("/engine/raw/{path:path}", responses=_RAW)
async def raw_delete(state: State, request: Request, path: str) -> Response:
    """Raw DELETE (e.g. `v1/responses/{id}`), as `raw_post`."""
    return await state.proxy.raw(request, "DELETE", path)  # type: ignore[no-any-return]

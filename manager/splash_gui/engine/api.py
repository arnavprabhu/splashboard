"""Engine routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response

from ..errors import ApiError, error_responses
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

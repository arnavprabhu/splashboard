"""Engine routes (SPEC §14 Engine). The supervisor (§6.3–§6.6) fills these in."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from ..errors import STUB_RESPONSES, ApiError, error_responses, not_implemented
from ..schemas import EngineDiscoveryInfo, EngineView, LoadRequest, RestartInfo
from ..state import ManagerState, get_state

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


def stopped_view(state: ManagerState) -> EngineView:
    """The view while no supervisor is attached: stopped, with discovery facts."""
    return EngineView(
        state="stopped",
        since=datetime.now(UTC).isoformat(),
        restart=RestartInfo(auto_restart=state.settings.current.global_.lifecycle.auto_restart),
        engine=EngineDiscoveryInfo.model_validate(state.engine().as_dict()),
    )


@router.get("/engine", response_model=EngineView)
def get_engine(state: State) -> EngineView:
    return stopped_view(state)


@router.post(
    "/engine/load",
    response_model=EngineView,
    status_code=202,
    responses={**STUB_RESPONSES, **error_responses(400, 404, 409, 503)},
)
def load(body: LoadRequest) -> EngineView:
    not_implemented("Engine load")


@router.post("/engine/stop", response_model=EngineView, responses=STUB_RESPONSES)
def stop() -> EngineView:
    not_implemented("Engine stop")


@router.post(
    "/engine/restart",
    response_model=EngineView,
    status_code=202,
    responses={**STUB_RESPONSES, **error_responses(409)},
)
def restart() -> EngineView:
    not_implemented("Engine restart")


@router.get("/engine/status", response_model=dict[str, Any], responses=error_responses(503))
def raw_status() -> dict[str, Any]:
    raise ApiError(
        503, "The engine is not running", "engine_unavailable", headers={"Retry-After": "5"}
    )


_RAW = {**STUB_RESPONSES, **error_responses(503)}


@router.post("/engine/raw/{path:path}", responses=_RAW)
def raw_post(path: str) -> Any:
    """Playground "raw to engine": forwards to the engine with the internal key, no injection."""
    not_implemented("Raw engine passthrough")


@router.get("/engine/raw/{path:path}", responses=_RAW)
def raw_get(path: str) -> Any:
    """Raw GET (e.g. `v1/responses/{id}`), as `raw_post`."""
    not_implemented("Raw engine passthrough")


@router.delete("/engine/raw/{path:path}", responses=_RAW)
def raw_delete(path: str) -> Any:
    """Raw DELETE (e.g. `v1/responses/{id}`), as `raw_post`."""
    not_implemented("Raw engine passthrough")

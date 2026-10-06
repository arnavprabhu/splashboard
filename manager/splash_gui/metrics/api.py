"""Live metrics."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from ..errors import SSE_RESPONSES
from ..schemas import LiveMetrics, MetricsSeries, OkResponse
from ..sse import sse_response
from ..state import ManagerState, get_state

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


@router.get("/metrics/live", response_class=StreamingResponse, responses=SSE_RESPONSES)
def live(state: State) -> StreamingResponse:
    return sse_response(state.metrics.stream())


@router.get("/metrics/snapshot", response_model=LiveMetrics)
async def snapshot(state: State) -> LiveMetrics:
    """One sample now (CLI `status`, G24): polls /status when the engine runs."""
    sup = state.supervisor
    if sup.accepting:
        await sup.poll_now()
    return state.metrics.current()  # type: ignore[no-any-return]


@router.get("/metrics/series", response_model=MetricsSeries)
def series(state: State, window: Annotated[int, Query(ge=1, le=3600)] = 900) -> MetricsSeries:
    return state.metrics.series(window)  # type: ignore[no-any-return]


@router.post("/metrics/reset", response_model=OkResponse)
def reset(state: State) -> OkResponse:
    """Reset the GUI's own "since engine start" counters (not Splash's)."""
    state.metrics.reset()
    return OkResponse()

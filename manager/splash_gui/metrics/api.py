"""Live metrics (SPEC §14 Metrics, §16.1)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from ..errors import SSE_RESPONSES, STUB_RESPONSES, not_implemented
from ..schemas import MetricsSeries, OkResponse

router = APIRouter()


@router.get(
    "/metrics/live", response_class=StreamingResponse, responses={**SSE_RESPONSES, **STUB_RESPONSES}
)
def live() -> StreamingResponse:
    not_implemented("Live metrics")


@router.get("/metrics/series", response_model=MetricsSeries, responses=STUB_RESPONSES)
def series(window: Annotated[int, Query(ge=1, le=3600)] = 900) -> MetricsSeries:
    not_implemented("Metrics series")


@router.post("/metrics/reset", response_model=OkResponse, responses=STUB_RESPONSES)
def reset() -> OkResponse:
    """Reset the GUI's own "since engine start" counters (not Splash's)."""
    not_implemented("Metrics reset")

"""Usage history (SPEC §14 Usage, §15.3, §16.2)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response

from ..errors import STUB_RESPONSES, not_implemented
from ..schemas import DeleteCount, UsageRows, UsageSummary, UsageTimeseries

router = APIRouter()


@router.get("/usage/summary", response_model=UsageSummary, responses=STUB_RESPONSES)
def summary(scope: Literal["all", "session"] = "all", model: str | None = None) -> UsageSummary:
    not_implemented("Usage summary")


@router.get("/usage/timeseries", response_model=UsageTimeseries, responses=STUB_RESPONSES)
def timeseries(
    bucket: Literal["minute", "hour", "day"] = "day",
    group_by: Literal["none", "model", "client", "endpoint"] = "model",
    start: str | None = None,
    end: str | None = None,
    model: str | None = None,
    view: Literal["series", "heatmap"] = "series",
) -> UsageTimeseries:
    not_implemented("Usage timeseries")


@router.get("/usage/requests", response_model=UsageRows, responses=STUB_RESPONSES)
def requests(
    model: str | None = None,
    endpoint: str | None = None,
    status: Annotated[str | None, Query(description="code, or 2xx/4xx/5xx")] = None,
    client: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str | None = None,
) -> UsageRows:
    not_implemented("Request log")


@router.get(
    "/usage/export.csv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}, **STUB_RESPONSES},
)
def export_csv(
    model: str | None = None,
    endpoint: str | None = None,
    status: str | None = None,
    client: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> Response:
    not_implemented("Usage export")


@router.delete("/usage", response_model=DeleteCount, responses=STUB_RESPONSES)
def clear() -> DeleteCount:
    not_implemented("Clear usage")

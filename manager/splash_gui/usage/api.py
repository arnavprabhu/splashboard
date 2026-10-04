"""Usage history and CSV export (SPEC §15.3, §16.2)."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response

from ..errors import ApiError
from ..schemas import DeleteCount, UsagePoint, UsageRow, UsageRows, UsageSummary, UsageTimeseries
from ..state import ManagerState, get_state
from .db import RequestFilter, iso

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


def checked_filter(**kwargs: Any) -> RequestFilter:
    flt = RequestFilter(**kwargs)
    try:
        flt.where()
    except ValueError as error:
        raise ApiError(400, str(error), "invalid_request") from None
    return flt


@router.get("/usage/summary", response_model=UsageSummary)
def summary(
    state: State, scope: Literal["all", "session", "today"] = "all", model: str | None = None
) -> UsageSummary:
    since = None
    if scope == "session":
        sessions = state.usage.sessions(limit=1)
        since = sessions[0]["started_at"] if sessions else iso(datetime.now(UTC))
    elif scope == "today":
        since = iso(datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0))
    return UsageSummary(
        scope=scope, since=since, **state.usage.summary(checked_filter(model=model, start=since))
    )


@router.get("/usage/timeseries", response_model=UsageTimeseries)
def timeseries(
    state: State,
    bucket: Literal["minute", "hour", "day"] = "day",
    group_by: Literal["none", "model", "client", "endpoint"] = "model",
    start: str | None = None,
    end: str | None = None,
    model: str | None = None,
    view: Literal["series", "heatmap"] = "series",
) -> UsageTimeseries:
    end = end or iso()
    start = start or iso(datetime.now(UTC) - timedelta(days=30))
    rows = state.usage.iter_requests(checked_filter(start=start, end=end, model=model))
    points: dict[tuple[str, str | None], UsagePoint] = {}
    heatmap = [[0] * 24 for _ in range(7)]
    for row in rows:
        dt = datetime.fromisoformat(row["ts"])
        local = dt.astimezone()
        heatmap[local.weekday()][local.hour] += 1
        dt = dt.replace(second=0, microsecond=0)
        if bucket in ("hour", "day"):
            dt = dt.replace(minute=0)
        if bucket == "day":
            dt = dt.replace(hour=0)
        group = None if group_by == "none" else row.get(group_by)
        key = (iso(dt), group)
        if key not in points:
            points[key] = UsagePoint(
                t=key[0],
                group=group,
                requests=0,
                prompt_tokens=0,
                cached_tokens=0,
                completion_tokens=0,
            )
        point = points[key]
        point.requests += 1
        point.errors += int(row["status"] >= 400)
        point.prompt_tokens += row.get("prompt_tokens") or 0
        point.cached_tokens += row.get("cached_tokens") or 0
        point.completion_tokens += row.get("completion_tokens") or 0
    return UsageTimeseries(
        bucket=bucket,
        group_by=group_by,
        start=start,
        end=end,
        points=sorted(points.values(), key=lambda p: (p.t, p.group or "")),
        heatmap=heatmap if view == "heatmap" else None,
    )


@router.get("/usage/requests", response_model=UsageRows)
def requests(
    state: State,
    model: str | None = None,
    endpoint: str | None = None,
    status: str | None = None,
    client: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str | None = None,
) -> UsageRows:
    if cursor is not None and (not cursor.isdigit() or int(cursor) <= 0):
        raise ApiError(400, "Invalid request cursor", "invalid_request")
    flt = checked_filter(
        model=model, endpoint=endpoint, status=status, client=client, start=start, end=end
    )
    rows = state.usage.requests(flt, limit + 1, int(cursor) if cursor else None)
    return UsageRows(
        rows=[UsageRow.model_validate(row) for row in rows[:limit]],
        next_cursor=str(rows[limit - 1]["id"]) if len(rows) > limit else None,
    )


@router.get("/usage/export.csv", response_class=Response)
def export_csv(
    state: State,
    model: str | None = None,
    endpoint: str | None = None,
    status: str | None = None,
    client: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> Response:
    rows = state.usage.iter_requests(
        checked_filter(
            model=model, endpoint=endpoint, status=status, client=client, start=start, end=end
        )
    )
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(UsageRow.model_fields))
    writer.writeheader()
    for row in rows:
        data = UsageRow.model_validate(row).model_dump()
        data["injected"] = json.dumps(data["injected"]) if data["injected"] else ""
        # Spreadsheet applications must treat client-supplied values as text.
        for key, value in data.items():
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                data[key] = "'" + value
        writer.writerow(data)
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="splash-usage.csv"'},
    )


@router.delete("/usage", response_model=DeleteCount)
def clear(state: State) -> DeleteCount:
    return DeleteCount(deleted=state.usage.delete_requests())

"""Usage history and CSV export."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response

from ..errors import ApiError
from ..schemas import (
    DeleteCount,
    UsageFacets,
    UsagePoint,
    UsageRow,
    UsageRows,
    UsageSummary,
    UsageTimeseries,
)
from ..state import ManagerState, get_state
from .db import CANCELLED, RequestFilter, iso, normalize_ts

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


def checked_filter(**kwargs: Any) -> RequestFilter:
    flt = RequestFilter(**kwargs)
    try:
        flt.where()
    except ValueError as error:
        raise ApiError(400, str(error), "invalid_request") from None
    return flt


@dataclass
class Filters:
    """The history filters every usage route takes.

    `start`/`end` are ISO 8601 timestamps or dates; `from`/`to` are accepted as
    aliases. `status` is a code (`404`), a class (`2xx`/`4xx`/`5xx`) or `cancelled`.
    """

    model: str | None = None
    endpoint: str | None = None
    status: str | None = None
    client: str | None = None
    start: str | None = None
    end: str | None = None
    request_id: str | None = None

    def request_filter(self, **override: Any) -> RequestFilter:
        values = {**self.__dict__, **override}
        return checked_filter(**values)


def filters(
    model: str | None = None,
    endpoint: str | None = None,
    status: str | None = None,
    client: str | None = None,
    start: str | None = None,
    end: str | None = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    request_id: str | None = None,
) -> Filters:
    return Filters(
        model=model or None,
        endpoint=endpoint or None,
        status=status or None,
        client=client or None,
        start=start or from_ or None,
        end=end or to or None,
        request_id=request_id or None,
    )


FilterDep = Annotated[Filters, Depends(filters)]


def _later(a: str | None, b: str | None) -> str | None:
    if a is None or b is None:
        return a or b
    try:
        return a if normalize_ts(a) >= normalize_ts(b) else b
    except ValueError:
        raise ApiError(400, f"invalid timestamp {a!r}", "invalid_request") from None


@router.get("/usage/summary", response_model=UsageSummary)
def summary(
    state: State, flt: FilterDep, scope: Literal["all", "session", "today"] = "all"
) -> UsageSummary:
    """Totals for the Status numbers band (`scope`) and the history Totals band
    (the filters). With a scope other than `all`, the later of the scope's start
    and `start` applies."""
    since = None
    if scope == "session":
        sessions = state.usage.sessions(limit=1)
        since = sessions[0]["started_at"] if sessions else iso(datetime.now(UTC))
    elif scope == "today":
        since = iso(datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0))
    start = _later(since, flt.start)
    return UsageSummary(
        scope=scope,
        since=since,
        start=start,
        end=flt.end,
        **state.usage.summary(flt.request_filter(start=start)),
    )


def bucket_start(dt: datetime, bucket: str) -> datetime:
    """The start of `dt`'s bucket. `hour` and `day` are the Mac's local hours and days
    (local day bounds), as the heatmap; `minute` is unaffected.
    - hour: `dt` in local time floored to the hour, keeping its own UTC offset, so
      the repeated 01:00 hour of a daylight-saving fall-back night is two buckets
      (01:00 CDT and 01:00 CST), never merged;
    - day: local midnight, given the offset in force at midnight (a day that changes
      daylight saving time starts at its own midnight)."""
    if bucket == "minute":
        return dt.astimezone(UTC).replace(second=0, microsecond=0)
    local = dt.astimezone()
    if bucket == "hour":
        return local.replace(minute=0, second=0, microsecond=0)
    midnight = local.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone()  # naive → the local zone's offset at that wall time


@router.get("/usage/timeseries", response_model=UsageTimeseries)
def timeseries(
    state: State,
    flt: FilterDep,
    bucket: Literal["minute", "hour", "day"] = "day",
    group_by: Literal["none", "model", "client", "endpoint"] | None = None,
    group: Literal["none", "model", "client", "endpoint"] | None = None,
    view: Literal["series", "heatmap"] = "series",
) -> UsageTimeseries:
    """Points per bucket (tokens per day per model, requests over time) and, with
    `view=heatmap`, the 7 × 24 local-time grid. `group` is an alias of `group_by`.
    Without `start`, the window is the last 30 days. `hour`/`day` points are local
    buckets: `t` is the local bucket start with its UTC offset."""
    grouping = group_by or group or "model"
    end = flt.end or iso()
    start = flt.start or iso(datetime.now(UTC) - timedelta(days=30))
    rows = state.usage.iter_requests(flt.request_filter(start=start, end=end))
    points: dict[tuple[datetime, str | None], UsagePoint] = {}
    heatmap = [[0] * 24 for _ in range(7)]
    heatmap_tokens = [[0] * 24 for _ in range(7)]
    for row in rows:
        dt = datetime.fromisoformat(row["ts"])
        local = dt.astimezone()
        tokens = (row.get("prompt_tokens") or 0) + (row.get("completion_tokens") or 0)
        heatmap[local.weekday()][local.hour] += 1
        heatmap_tokens[local.weekday()][local.hour] += tokens
        begins = bucket_start(dt, bucket)
        key_group = None if grouping == "none" else row.get(grouping)
        key = (begins, key_group)
        if key not in points:
            points[key] = UsagePoint(
                t=iso(begins) if bucket == "minute" else begins.isoformat(),
                group=key_group,
                requests=0,
                prompt_tokens=0,
                cached_tokens=0,
                completion_tokens=0,
            )
        point = points[key]
        point.requests += 1
        cancelled = row.get("error_code") == CANCELLED
        point.cancelled += int(cancelled)
        point.errors += int(row["status"] >= 400 and not cancelled)
        point.prompt_tokens += row.get("prompt_tokens") or 0
        point.cached_tokens += row.get("cached_tokens") or 0
        point.completion_tokens += row.get("completion_tokens") or 0
    return UsageTimeseries(
        bucket=bucket,
        group_by=grouping,
        start=start,
        end=end,
        points=[points[k] for k in sorted(points, key=lambda k: (k[0], k[1] or ""))],
        heatmap=heatmap if view == "heatmap" else None,
        heatmap_tokens=heatmap_tokens if view == "heatmap" else None,
    )


@router.get("/usage/requests", response_model=UsageRows)
def requests(
    state: State,
    flt: FilterDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str | None = None,
    page: Annotated[int | None, Query(ge=1)] = None,
) -> UsageRows:
    """The request log, newest first. Page with `page` (1-based, `offset` and
    `total` give the row range) or with `cursor` (`next_cursor` of the last page)."""
    if cursor is not None and (not cursor.isdigit() or int(cursor) <= 0):
        raise ApiError(400, "Invalid request cursor", "invalid_request")
    if cursor is not None and page is not None:
        raise ApiError(400, "Use either page or cursor, not both", "invalid_request")
    request_filter = flt.request_filter()
    total = state.usage.request_count(request_filter)
    offset: int | None
    if page is not None:
        offset = (page - 1) * limit
        rows = state.usage.requests_page(request_filter, limit + 1, offset)
    else:
        offset = None if cursor else 0
        rows = state.usage.requests(request_filter, limit + 1, int(cursor) if cursor else None)
    return UsageRows(
        rows=[UsageRow.model_validate(row) for row in rows[:limit]],
        next_cursor=str(rows[limit - 1]["id"]) if len(rows) > limit else None,
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/usage/facets", response_model=UsageFacets)
def facets(state: State, flt: FilterDep) -> UsageFacets:
    """Distinct models, endpoints, clients and profiles in range (the filters' options).
    Takes the same filters; pass only `start`/`end` for every option in the range."""
    return UsageFacets(**state.usage.facets(flt.request_filter()))


@router.get("/usage/export.csv", response_class=Response)
def export_csv(state: State, flt: FilterDep) -> Response:
    rows = state.usage.iter_requests(flt.request_filter())
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

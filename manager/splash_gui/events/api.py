"""Events and alerts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from ..errors import SSE_RESPONSES, ApiError, error_responses
from ..schemas import AlertList, HelloEvent, JobList, JobView, OkResponse
from ..sse import sse_response
from ..state import ManagerState, get_state
from .alerts import MENUBAR_CLIENT
from .bus import stream

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


def hello(state: ManagerState) -> HelloEvent:
    downloads = list(state.downloads.items.values()) if state.downloads is not None else []
    return HelloEvent(
        server_time=datetime.now(UTC).isoformat(),
        engine=state.supervisor.view(),
        alerts=state.alerts.all(),
        downloads=downloads,
    )


@router.get("/events", response_class=StreamingResponse, responses=SSE_RESPONSES)
def events(
    state: State,
    client: Annotated[str | None, Query(max_length=32, description="`menubar` for the app")] = None,
) -> StreamingResponse:
    def snapshot() -> list[tuple[str, Any]]:
        return [("hello", hello(state))]

    return sse_response(stream(state.events, snapshot, client=client))


@router.post(
    "/app/check-updates", status_code=202, response_model=OkResponse, responses=error_responses(409)
)
def check_app_updates(state: State) -> OkResponse:
    """Ask the menu bar app to run its Sparkle check.

    The check runs in the app, so the manager only relays it as an `app.check_updates`
    event to the event streams opened with `client=menubar`. With none open, 409
    `app_not_running`: the web then says `Open the menu bar app to update.`"""
    if state.events.subscriber_count(MENUBAR_CLIENT) == 0:
        raise ApiError(409, "Open the menu bar app to update.", "app_not_running")
    state.events.publish("app.check_updates", {})
    return OkResponse()


@router.get("/alerts", response_model=AlertList)
def alerts(state: State) -> AlertList:
    return AlertList(alerts=state.alerts.all())


@router.post(
    "/alerts/{alert_id}/dismiss", response_model=OkResponse, responses=error_responses(404, 409)
)
def dismiss(state: State, alert_id: str) -> OkResponse:
    outcome = state.alerts.dismiss(alert_id)
    if outcome == "not_found":
        raise ApiError(404, f"no alert {alert_id}", "alert_not_found")
    if outcome == "not_dismissible":
        raise ApiError(409, "This alert can't be dismissed; resolve it instead", "not_dismissible")
    return OkResponse()


@router.get("/jobs", response_model=JobList)
def jobs(state: State) -> JobList:
    """Background jobs since the manager started (verify, storage moves, imports,
    engine install/upgrade), oldest first. Progress also streams as `job` events."""
    return JobList(jobs=[job.view() for job in state.jobs.all()])


@router.get("/jobs/{job_id}", response_model=JobView, responses=error_responses(404))
def job(state: State, job_id: str) -> JobView:
    found = state.jobs.get(job_id)
    if found is None:
        raise ApiError(404, f"no job {job_id}", "job_not_found")
    return found.view()

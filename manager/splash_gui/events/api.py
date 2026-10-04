"""Events and alerts (SPEC §14 Events, §16.3). SSE event names are in docs/api.md."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..errors import SSE_RESPONSES, STUB_RESPONSES, not_implemented
from ..schemas import AlertList, OkResponse

router = APIRouter()


@router.get(
    "/events", response_class=StreamingResponse, responses={**SSE_RESPONSES, **STUB_RESPONSES}
)
def events() -> StreamingResponse:
    not_implemented("Event stream")


@router.get("/alerts", response_model=AlertList, responses=STUB_RESPONSES)
def alerts() -> AlertList:
    not_implemented("Alerts")


@router.post("/alerts/{alert_id}/dismiss", response_model=OkResponse, responses=STUB_RESPONSES)
def dismiss(alert_id: str) -> OkResponse:
    not_implemented("Alerts")

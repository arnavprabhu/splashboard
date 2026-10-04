"""Integrations (SPEC §14 Integrations, §10.7, §11)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter

from ..errors import STUB_RESPONSES, error_responses, not_implemented
from ..schemas import (
    ConnectRequest,
    DesktopIntegration,
    EntriesRemoved,
    Integrations,
    OpenTerminalRequest,
    OpenTerminalResult,
    RestoreAllResult,
)

router = APIRouter()
DesktopName = Literal["claude-desktop", "codex-app"]
CliName = Literal["claude", "codex", "opencode", "hermes", "pi"]
_ERR = {**STUB_RESPONSES, **error_responses(404, 409)}


@router.get("/integrations", response_model=Integrations, responses=STUB_RESPONSES)
def list_integrations() -> Integrations:
    not_implemented("Integrations")


@router.post("/integrations/restore-all", response_model=RestoreAllResult, responses=STUB_RESPONSES)
def restore_all() -> RestoreAllResult:
    not_implemented("Integrations restore")


@router.post("/integrations/{name}/connect", response_model=DesktopIntegration, responses=_ERR)
def connect(name: DesktopName, body: ConnectRequest | None = None) -> DesktopIntegration:
    not_implemented("Desktop integrations")


@router.post("/integrations/{name}/disconnect", response_model=DesktopIntegration, responses=_ERR)
def disconnect(name: DesktopName) -> DesktopIntegration:
    not_implemented("Desktop integrations")


@router.post(
    "/integrations/{name}/open-terminal", response_model=OpenTerminalResult, responses=_ERR
)
def open_terminal(name: CliName, body: OpenTerminalRequest | None = None) -> OpenTerminalResult:
    not_implemented("Open in Terminal")


@router.delete("/integrations/{name}/entries", response_model=EntriesRemoved, responses=_ERR)
def remove_entries(name: Literal["hermes", "pi"]) -> EntriesRemoved:
    not_implemented("Integration entry cleanup")

"""Integrations."""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Query

from ..errors import error_responses
from ..schemas import (
    ConnectRequest,
    DesktopIntegration,
    EntriesRemoved,
    Integrations,
    LaunchPrint,
    OpenedPath,
    OpenTerminalRequest,
    OpenTerminalResult,
    RestoreAllResult,
)
from ..state import ManagerState, get_state
from .service import IntegrationsService

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]
DesktopName = Literal["claude-desktop", "codex-app"]
CliName = Literal["claude", "codex", "opencode", "hermes", "pi"]
_ERR = error_responses(404, 409)


@router.post("/integrations", response_model=Integrations, responses=_ERR)
def list_integrations(state: State) -> Integrations:
    return cast(IntegrationsService, state.integrations).listing()


@router.post("/integrations/restore-all", response_model=RestoreAllResult, responses=_ERR)
async def restore_all(state: State) -> RestoreAllResult:
    return await cast(IntegrationsService, state.integrations).restore_all()


@router.post("/integrations/{name}/connect", response_model=DesktopIntegration, responses=_ERR)
async def connect(
    state: State, name: DesktopName, body: ConnectRequest | None = None
) -> DesktopIntegration:
    return await cast(IntegrationsService, state.integrations).connect(
        name, bool(body and body.confirm_restart)
    )


@router.post("/integrations/{name}/disconnect", response_model=DesktopIntegration, responses=_ERR)
async def disconnect(state: State, name: DesktopName) -> DesktopIntegration:
    return await cast(IntegrationsService, state.integrations).disconnect(name)


@router.post(
    "/integrations/{name}/open-terminal", response_model=OpenTerminalResult, responses=_ERR
)
def open_terminal(
    state: State, name: CliName, body: OpenTerminalRequest | None = None
) -> OpenTerminalResult:
    return cast(IntegrationsService, state.integrations).open_terminal(
        name, body.model if body else None
    )


@router.delete("/integrations/{name}/entries", response_model=EntriesRemoved, responses=_ERR)
def remove_entries(state: State, name: Literal["hermes", "pi"]) -> EntriesRemoved:
    return cast(IntegrationsService, state.integrations).remove_entries(name)


@router.post(
    "/integrations/{name}/open", response_model=OpenedPath, responses=error_responses(404, 503)
)
def open_app(state: State, name: DesktopName) -> OpenedPath:
    """Open app: `open -a Claude`, or the Codex app on a new thread."""
    return cast(IntegrationsService, state.integrations).open(name)


@router.post(
    "/integrations/{name}/reveal-backup",
    response_model=OpenedPath,
    responses=error_responses(404, 503),
)
def reveal_backup(
    state: State, name: Literal["claude-desktop", "codex-app", "hermes", "pi"]
) -> OpenedPath:
    """View backup: the newest `integrations/backups/<name>/<time>` folder in Finder."""
    return cast(IntegrationsService, state.integrations).reveal_backup(name)


@router.post("/integrations/{name}/print", response_model=LaunchPrint)
def print_launch(
    state: State,
    name: CliName,
    model: Annotated[str | None, Query(description="ID or ID:profile; default active")] = None,
) -> LaunchPrint:
    """`splash launch <name> --print` as data, for "What this changes"."""
    return cast(IntegrationsService, state.integrations).print_launch(name, model)

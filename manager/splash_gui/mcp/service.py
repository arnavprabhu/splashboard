"""Lifecycle hook for the MCP subsystem (see splash_gui/service.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .manager import McpManager

if TYPE_CHECKING:
    from ..service import Service
    from ..state import ManagerState


def create(state: ManagerState) -> Service | None:
    """MCP connections for the chat (SPEC §10.5), attached as `state.mcp`."""
    manager = McpManager(state)
    state.mcp = manager
    return manager

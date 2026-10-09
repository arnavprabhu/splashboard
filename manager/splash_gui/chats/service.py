"""Lifecycle hook for the chats subsystem (see splash_gui/service.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .store import ChatStore

if TYPE_CHECKING:
    from ..service import Service
    from ..state import ManagerState


def create(state: ManagerState) -> Service | None:
    """The chat store, attached as `state.chats`."""
    store = ChatStore(state.paths.chats_dir, state.usage)
    state.chats = store
    return store

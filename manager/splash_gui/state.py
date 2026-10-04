"""Shared manager state, reachable from routes as `request.app.state.manager`.

Other subsystems (supervisor, downloader, proxy, usage) attach themselves here
and replace the provider callables with real ones.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Request

from .auth.core import AuthManager
from .engine.discovery import EngineInfo, discover
from .engine.serve_options import EngineOptionsCache
from .paths import SPLASH_CRASH_TRACE_DIR, Paths
from .secrets import SecretStore
from .settings.store import Change, SettingsStore

DISCOVERY_TTL_S = 60.0


def _physical_memory() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        return 0


@dataclass
class ManagerState:
    paths: Paths
    settings: SettingsStore
    secrets: SecretStore
    web_dist: Path | None = None
    auth: AuthManager = field(init=False)
    engine_options: EngineOptionsCache = field(default_factory=EngineOptionsCache)
    discover_engine: Callable[[], EngineInfo] | None = None
    # Providers other tracks replace: the model the engine is serving (or None),
    # installed model IDs (None = unknown), and unified memory in bytes.
    active_model: Callable[[], str | None] = lambda: None
    installed_models: Callable[[], frozenset[str] | None] = lambda: None
    memory_bytes: Callable[[], int] = _physical_memory
    # The engine's raw /status JSON, or None while it isn't running (the supervisor sets it).
    raw_status: Callable[[], dict[str, Any] | None] = lambda: None
    # Where Splash writes crash traces (hardcoded by Splash; overridable for tests).
    crash_trace_dir: Path = SPLASH_CRASH_TRACE_DIR
    # Called after every successful settings save with (changes, restart_required).
    # The address the public port is listening on (set by the runner; None in tests).
    bound: tuple[str, int] | None = None
    settings_listeners: list[Callable[[list[Change], bool], None]] = field(default_factory=list)
    _engine: EngineInfo | None = None
    _engine_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.auth = AuthManager(self.paths, self.secrets)

    def engine(self, refresh: bool = False) -> EngineInfo:
        """Discovery result, cached for a minute (cheap enough to call per request)."""
        with self._lock:
            fresh = time.monotonic() - self._engine_at < DISCOVERY_TTL_S
            if self._engine is not None and fresh and not refresh:
                return self._engine
        if self.discover_engine is not None:
            info = self.discover_engine()
        else:
            info = discover(
                self.settings.current.global_.engine.path, shim_paths=(self.paths.shim,)
            )
        with self._lock:
            self._engine, self._engine_at = info, time.monotonic()
        return info


def get_state(request: Request) -> ManagerState:
    state = request.app.state.manager
    assert isinstance(state, ManagerState)
    return state

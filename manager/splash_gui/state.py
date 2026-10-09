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

from starlette.requests import HTTPConnection

from . import paths as _paths
from .auth.core import AuthManager
from .engine.discovery import EngineInfo, discover
from .engine.serve_options import EngineOptionsCache
from .events.alerts import AlertCenter
from .events.bus import EventBus
from .jobs import Jobs
from .paths import Paths
from .secrets import SecretStore
from .settings.store import Change, SettingsStore
from .system.macos import MacOS
from .usage.db import UsageDB

DISCOVERY_TTL_S = 60.0
UPDATE_CHECK_ENV = "SPLASH_GUI_UPDATE_CHECK"


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
    # Where Splash writes crash traces (hardcoded by Splash). Read from the module at
    # construction so the test suite can point it away from ~/Library/Logs.
    crash_trace_dir: Path = field(default_factory=lambda: _paths.SPLASH_CRASH_TRACE_DIR)
    # Called after every successful settings save with (changes, restart_required).
    # The address the public port is listening on (set by the runner; None in tests).
    bound: tuple[str, int] | None = None
    settings_listeners: list[Callable[[list[Change], bool], None]] = field(default_factory=list)
    # Facts about an installed model for summaries (draft, format, …); the models
    # track replaces it.
    model_info: Callable[[str], dict[str, Any] | None] = lambda _model: None
    # Admin clients watching live data outside /events (e.g. /metrics/live streams);
    # the supervisor polls /status every second while this or /events has a client.
    watchers: int = 0
    # Background-check switches (tests turn the network ones off).
    update_check: bool = field(default_factory=lambda: os.environ.get(UPDATE_CHECK_ENV, "1") != "0")
    events: EventBus = field(default_factory=EventBus)
    alerts: AlertCenter = field(init=False)
    jobs: Jobs = field(init=False)
    usage: UsageDB = field(init=False)
    # Subsystems attach themselves here (typed as Any to keep imports acyclic).
    supervisor: Any = None
    proxy: Any = None
    metrics: Any = None
    models: Any = None
    downloads: Any = None
    chats: Any = None
    mcp: Any = None
    benchmark: Any = None
    integrations: Any = None
    updates: Any = None
    macos: MacOS = field(default_factory=MacOS)
    # The home directory whose desktop-app configs integrations edit (None: the
    # user's real home). Tests point it at a throwaway directory.
    user_home: Path | None = None
    # Running crash-trace replays by trace name (POST /traces/{name}/replay/cancel).
    replays: dict[str, Any] = field(default_factory=dict)
    # Called to stop the whole manager (POST /shutdown); the runner sets it.
    request_shutdown: Callable[[], None] | None = None
    # Set once `POST /uninstall` deleted the data folder: nothing writes it again.
    data_removed: bool = False
    started_at: float = field(default_factory=time.time)
    _engine: EngineInfo | None = None
    _engine_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.auth = AuthManager(self.paths, self.secrets)
        self.alerts = AlertCenter(self.events, self.settings)
        self.jobs = Jobs(self.events)
        self.usage = UsageDB(self.paths.usage_db)

    def engine_cached(self) -> EngineInfo:
        """The last discovery result without refreshing (discovering only if none)."""
        with self._lock:
            if self._engine is not None:
                return self._engine
        return self.engine()

    def forget_engine(self) -> None:
        with self._lock:
            self._engine, self._engine_at = None, 0.0

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


def get_state(request: HTTPConnection) -> ManagerState:
    state = request.app.state.manager
    assert isinstance(state, ManagerState)
    return state

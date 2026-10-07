"""Engine supervision (SPEC §6.2–§6.6): one `splash serve` process at a time.

- **Start**: the exact command from `flags.build_launch_for_model`, its own process
  group, a random internal key in the environment, stdout/stderr captured line by
  line into engine.log, the live log and the startup parser (§6.4).
- **States** (§6.3, docs/api.md §3.1): stopped → starting(installing → loading →
  warming) → ready ⇄ busy, plus idle_released, recovering, engine_failed,
  stopping, crashed and failed.
- **Stop** (§6.5): SIGINT to the group, 15 s, a second SIGINT, 5 s, SIGKILL.
- **Crash** (§6.5): restart after 1 s, then 5 s, then 30 s; 3 crashes within
  5 minutes → failed + alert.
- **Polling** (§6.6): /ready every 2 s while starting (sooner after the Ready
  line); /status every 1 s while someone watches, 5 s otherwise, never while
  stopped. A counter drop or a new identity build id means Splash restarted.
- **Idle unload** (§6.5): stop the process after `lifecycle.idle_unload_minutes`
  without API requests.
- **Install progress** (§6.3 `starting.installing`): each `Fetching N file(s), …`
  line starts an `InstallTracker` sampled every second from the Hub cache and shown
  as `EngineView.install`; a load or restart then needs `force` (409
  `install_in_progress`), because stopping loses the file in progress (Q24).
- **Stale partials** (§9.4): at every start and every session end, the per-process
  `<etag>.<uuid8>.incomplete` files killed runs left in the model's repositories are
  deleted (never an open one, the legacy `<etag>.incomplete`, or one the Downloader
  still needs).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..errors import ApiError
from ..logging_setup import SESSION_END, SESSION_START, EngineLogWriter
from ..models import catalog as cat
from ..paths import ensure_private_dir, write_atomic
from ..schemas import (
    EngineDiscoveryInfo,
    EngineError,
    EngineNotice,
    EngineSuggestion,
    EngineView,
    NotificationAction,
    RestartInfo,
    TransportInfo,
)
from ..secrets import generate_internal_key
from ..settings import parsers as p
from ..settings.effective import effective_serve
from . import status as st
from .flags import INTERNAL_PORT_RANGE, LaunchError, LaunchSpec, build_launch_for_model
from .install import FetchLine, InstallTracker, parse_fetching, remove_stale_partials
from .startup import (
    ErrorEvent,
    Event,
    NoticeEvent,
    PhaseEvent,
    ReadyEvent,
    StartupParser,
    TakenBackEvent,
    TemplateEvent,
    TransportEvent,
    WeightsEvent,
)

if TYPE_CHECKING:
    from ..state import ManagerState

log = logging.getLogger(__name__)

RUNNING_STATES = frozenset(
    {"starting", "ready", "busy", "idle_released", "recovering", "engine_failed"}
)
SERVING_STATES = frozenset({"ready", "busy", "idle_released"})
LOG_TAIL = 20
NOTICE_LIMIT = 20
# Splash frees weight memory after 10 idle minutes by default (§3.3). Since 1.2.1
# /status says so itself (`weights.released`); the timer only covers an engine
# whose status has no `weights` block.
SPLASH_IDLE_RELEASE_S = 600.0


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class _Run:
    """One spawned engine process."""

    process: asyncio.subprocess.Process
    model: str
    port: int
    key: str
    spec: LaunchSpec
    session_id: int | None
    started_mono: float
    parser: StartupParser = field(default_factory=StartupParser)
    stop_reason: str | None = None
    ready_line: bool = False
    ready_polls_ok: int = 0
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    readers_done: asyncio.Event = field(default_factory=asyncio.Event)
    exit_code: int | None = None
    saved_identity: dict[str, Any] | None = None
    # Spawned by the crash auto-restart (D45: its startup crash is a crash).
    auto_restart: bool = False
    # What `starting.installing` is downloading (the latest `Fetching …` line).
    install: InstallTracker | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def free_port(preferred: range = INTERNAL_PORT_RANGE) -> int:
    """A free loopback port in 18000–18999 (SPEC §4.3), else any free port."""
    import random

    candidates = list(preferred)
    random.shuffle(candidates)
    for port in candidates[:200]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Supervisor:
    # Timings (SPEC §6.5/§6.6); tests shrink them.
    stop_timeout_s = 15.0
    second_sigint_s = 5.0
    backoff_s: tuple[float, ...] = (1.0, 5.0, 30.0)
    crash_window_s = 300.0
    crash_limit = 3
    ready_poll_s = 2.0
    ready_poll_after_line_s = 0.1
    status_fast_s = 1.0
    status_slow_s = 5.0
    idle_check_s = 15.0
    install_sample_s = 1.0
    splash_idle_release_s = SPLASH_IDLE_RELEASE_S

    def __init__(self, state: ManagerState) -> None:
        self.app = state
        self.state = "stopped"
        self.phase: str | None = None
        self.since = now_iso()
        self.model: str | None = None
        self.started_at: str | None = None
        self.ready_at: str | None = None
        self._ready_mono: float | None = None
        self.error: EngineError | None = None
        self.notices: list[EngineNotice] = []
        self.log_tail: deque[str] = deque(maxlen=LOG_TAIL)
        self.chat_template_mode: str | None = None
        self.max_context: int | None = None
        self.vision: bool | None = None
        self.status: dict[str, Any] | None = None
        self.status_at: float | None = None
        self.transport: TransportInfo | None = None
        self.schema_version: int | None = None
        self.engine_models: list[dict[str, Any]] = []
        self.taken_back: dict[str, int] | None = None
        self.restarts_detected = 0
        self._run: _Run | None = None
        self._crashes: deque[float] = deque()
        self._attempt = 0
        self._next_retry_at: str | None = None
        self._backoff: float | None = None
        self._restart_task: asyncio.Task[None] | None = None
        self._op = asyncio.Lock()
        self._changed = asyncio.Condition()
        self._client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._last_publish = 0.0
        self._publish_pending = False
        self.in_flight = 0
        self.last_request_mono = time.monotonic()
        self.status_listeners: list[Callable[[dict[str, Any] | None, bool], None]] = []
        self.state_listeners: list[Callable[[str, str], None]] = []
        self._log = EngineLogWriter(
            state.paths.engine_log, lambda: (*state.secrets.known_values(), *self._keys())
        )
        self._last_build_id: str | None = None
        self._last_submitted: int | None = None
        self._shutting_down = False
        # Operations that must not race an engine start (a model delete, a KV clear).
        self._holds: list[str] = []

    # Lifecycle of the supervisor itself -------------------------------------------

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=2.0))
        self._tasks.append(asyncio.create_task(self._status_loop(), name="engine-status"))
        self._tasks.append(asyncio.create_task(self._idle_loop(), name="engine-idle"))

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._restart_task is not None:
            self._restart_task.cancel()
        with contextlib.suppress(Exception):
            await self.stop(reason="manager_exit")
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        if self._client is not None:
            await self._client.aclose()
        self._log.close()

    def _keys(self) -> tuple[str, ...]:
        return (self._run.key,) if self._run else ()

    # Public facts used by the proxy and others --------------------------------------

    @property
    def base_url(self) -> str | None:
        return self._run.base_url if self._run else None

    @property
    def internal_key(self) -> str | None:
        return self._run.key if self._run else None

    @property
    def internal_port(self) -> int | None:
        return self._run.port if self._run else None

    @property
    def pid(self) -> int | None:
        return self._run.process.pid if self._run else None

    @property
    def serving(self) -> bool:
        return self.state in SERVING_STATES or self.state == "recovering"

    @property
    def accepting(self) -> bool:
        """Whether a request can be forwarded now."""
        return self._run is not None and self.state in SERVING_STATES | {
            "recovering",
            "engine_failed",
        }

    def active_model(self) -> str | None:
        return self.model if self.state in RUNNING_STATES else None

    def busy(self) -> bool:
        return self.in_flight > 0 or (self.status is not None and st.busy(self.status))

    def aliases(self) -> list[str]:
        """Every name the engine answers to (real ID first unless announced)."""
        return [str(m["id"]) for m in self.engine_models if isinstance(m.get("id"), str)]

    # View --------------------------------------------------------------------------

    def view(self) -> EngineView:
        engine = self.app.engine_cached()
        status = self.status
        scheduler_active = 0
        queued = 0
        if status is not None and self.state in SERVING_STATES:
            scheduler_active = (st.integer(status, "scheduler.prefilling") or 0) + (
                st.integer(status, "scheduler.decoding") or 0
            )
            queued = (st.integer(status, "scheduler.queued") or 0) + (
                st.integer(status, "scheduler.waiting_resources") or 0
            )
        kv_format = st.text(status, "identity.kv.format") if status else None
        if kv_format is None and self.model:
            with contextlib.suppress(Exception):
                kv_format = effective_serve(self.app.settings.current, self.model).kv_format
        settings = self.app.settings.current
        tracker = self._install_tracker()
        uptime = None
        if self._run is not None and self.state in RUNNING_STATES:
            uptime = round(time.monotonic() - self._run.started_mono, 1)
        return EngineView(
            state=self.state,  # type: ignore[arg-type]
            phase=self.phase if self.state == "starting" else None,  # type: ignore[arg-type]
            model=self.model,
            since=self.since,
            started_at=self.started_at,
            ready_at=self.ready_at,
            uptime_s=uptime,
            pid=self.pid if self.state in RUNNING_STATES | {"stopping"} else None,
            internal_port=self.internal_port if self.state in RUNNING_STATES else None,
            requests_in_flight=max(self.in_flight, scheduler_active),
            queued=queued,
            maximum_context_tokens=self.max_context,
            chat_template_mode=self.chat_template_mode,  # type: ignore[arg-type]
            kv_format=kv_format,
            vision=self.vision,
            draft=self._draft(),
            status_schema_version=self.schema_version,
            schema_supported=self.schema_version in (None, st.SCHEMA_VERSION),
            transport=self.transport,
            restart=RestartInfo(
                auto_restart=settings.global_.lifecycle.auto_restart,
                attempt=self._attempt,
                crashes_in_window=len(self._crashes_in_window()),
                next_retry_at=self._next_retry_at if self.state == "crashed" else None,
                backoff_s=self._backoff if self.state == "crashed" else None,
            ),
            error=self.error,
            notices=list(self.notices),
            log_tail=list(self.log_tail) if self.state in ("starting", "failed", "crashed") else [],
            command=self._run.spec.display() if self._run else None,
            taken_back=dict(self.taken_back) if self.taken_back is not None else None,
            persistent_cache=st.boolean(status, "disk.persistent") if status else None,
            install=tracker.view() if tracker is not None else None,
            engine=EngineDiscoveryInfo.model_validate(engine.as_dict()),
        )

    def _install_tracker(self) -> InstallTracker | None:
        run = self._run
        if run is None or self.state != "starting" or self.phase != "installing":
            return None
        return run.install

    def _draft(self) -> str | None:
        if not self.model:
            return None
        info = self.app.model_info(self.model)
        draft = info.get("draft") if info else None
        if isinstance(draft, dict) and draft.get("repo_id"):
            return str(draft["repo_id"])  # InstalledModel.draft is a DraftRef
        if isinstance(draft, str) and draft:
            return draft
        with contextlib.suppress(Exception):
            return effective_serve(self.app.settings.current, self.model).draft_model
        return None

    # State changes -------------------------------------------------------------------

    def _set(self, state: str, *, phase: str | None = None) -> None:
        previous = self.state
        changed = state != self.state or phase != self.phase
        if state != self.state:
            self.since = now_iso()
        self.state = state
        self.phase = phase if state == "starting" else None
        if self._run is not None and (self.state != "starting" or self.phase != "installing"):
            self._run.install = None
        if changed:
            for listener in list(self.state_listeners):
                try:
                    listener(previous, state)
                except Exception:
                    log.exception("engine state listener failed")
            self._publish(force=True)
            self._notify_waiters()

    def _notify_waiters(self) -> None:
        async def notify() -> None:
            async with self._changed:
                self._changed.notify_all()

        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(notify())

    def _publish(self, force: bool = False) -> None:
        """`engine.state` on every change; at most every 250 ms for log-tail updates."""
        now = time.monotonic()
        if force or now - self._last_publish >= 0.25:
            self._last_publish = now
            self._publish_pending = False
            self.app.events.publish("engine.state", self.view())
            return
        if not self._publish_pending:
            self._publish_pending = True
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().call_later(0.25, self._flush_publish)

    def _flush_publish(self) -> None:
        if self._publish_pending:
            self._publish(force=True)

    def _add_notice(self, kind: str, message: str, raw: str | None = None) -> None:
        notice = EngineNotice(kind=kind, message=message, raw=raw, ts=now_iso())  # type: ignore[arg-type]
        self.notices = [n for n in self.notices if not (n.kind == kind and n.message == message)]
        self.notices.append(notice)
        self.notices = self.notices[-NOTICE_LIMIT:]
        self._publish(force=True)

    async def wait_for(self, predicate: Callable[[], bool], timeout: float | None) -> bool:
        async def waiter() -> None:
            async with self._changed:
                await self._changed.wait_for(predicate)

        try:
            await asyncio.wait_for(waiter(), timeout)
        except TimeoutError:
            return predicate()
        return True

    async def wait_ready(self, timeout: float | None) -> bool:
        terminal = {"stopped", "failed"}
        await self.wait_for(lambda: self.state in SERVING_STATES or self.state in terminal, timeout)
        return self.state in SERVING_STATES

    # Load / stop / restart ---------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def hold(self, reason: str) -> AsyncIterator[None]:
        """Refuse every engine start until the block ends.

        A model delete or KV-cache clear stops the engine and then removes files; an
        auto-load request arriving in between would start the engine on files that
        are being deleted."""
        self._holds.append(reason)
        try:
            yield
        finally:
            self._holds.remove(reason)

    def _check_hold(self) -> None:
        if self._holds:
            raise ApiError(
                503,
                f"Splash GUI is busy ({self._holds[0].replace('_', ' ')}); try again shortly",
                "engine_held",
                headers={"Retry-After": "5"},
            )

    async def load(self, model: str, *, force: bool = False, reason: str = "load") -> EngineView:
        try:
            p.parse_model_id(model)
        except ValueError as error:
            raise ApiError(400, str(error), "invalid_model_id") from None
        installed = self.app.installed_models()
        if installed is not None and model not in installed:
            raise ApiError(
                404,
                f"{model} is not installed",
                "model_not_installed",
                details={"installed": sorted(installed)},
            )
        jobs = getattr(self.app, "jobs", None)
        if jobs is not None and (jobs.running("storage_move") or jobs.running("import")):
            # The models or cache directory is being moved under the engine's feet.
            raise ApiError(409, "Wait for the storage operation to finish", "storage_busy")
        self._check_hold()
        engine = await asyncio.to_thread(self.app.engine)
        if not engine.found or engine.cli is None:
            raise ApiError(503, engine.error or "Splash is not installed", "engine_not_found")
        async with self._op:
            self._check_hold()
            if (
                self.model == model
                and self.state in RUNNING_STATES
                and self.state not in ("engine_failed",)
            ):
                # Already loading or serving it (an install included): nothing stops,
                # and `wait` waits for it.
                return self.view()
            self._check_install(force)
            if self._run is not None and self.busy() and not force:
                raise ApiError(
                    409,
                    f"Splash GUI is serving {self.model}; switching models while requests are in "
                    "flight is disabled",
                    "model_switch_busy",
                    details={"active": self.model, "requests_in_flight": self.in_flight},
                )
            self._cancel_restart()
            if self._run is not None:
                await self._stop_locked("switch" if self.model != model else reason)
            self._crashes.clear()
            self._attempt = 0
            await self._spawn(model)
            return self.view()

    async def stop(self, reason: str = "stop") -> EngineView:
        async with self._op:
            self._cancel_restart()
            await self._stop_locked(reason)
            if self.state in ("crashed", "failed") and self._run is None:
                self.error = None
                self._set("stopped")
            return self.view()

    async def restart(self, *, force: bool = False) -> EngineView:
        model = self.model
        if model is None:
            raise ApiError(409, "No model is loaded; load one first", "engine_not_loaded")
        async with self._op:
            self._check_hold()
            self._check_install(force)
            if self._run is not None and self.busy() and not force:
                raise ApiError(
                    409,
                    "Requests are in flight; restart anyway with force",
                    "model_switch_busy",
                    details={"active": self.model, "requests_in_flight": self.in_flight},
                )
            self._cancel_restart()
            await self._stop_locked("restart")
            self._crashes.clear()
            self._attempt = 0
            await self._spawn(model)
            return self.view()

    def _check_install(self, force: bool) -> None:
        """A load of another model or a restart stops a `splash serve` that is
        downloading files; the hub Splash bundles cannot continue the file in progress
        (Q24), so ask first. A load of the model being installed never gets here."""
        tracker = self._install_tracker()
        if tracker is None or force:
            return
        view = tracker.view()
        raise ApiError(
            409,
            f"Splash is downloading {view.repo} for {self.model}; stopping it now restarts "
            "the file in progress from the beginning. Send force to stop it anyway",
            "install_in_progress",
            details={
                "active": self.model,
                "repo": view.repo,
                "done_bytes": view.done_bytes,
                "total_bytes": view.total_bytes,
            },
        )

    def _cancel_restart(self) -> None:
        if self._restart_task is not None and not self._restart_task.done():
            self._restart_task.cancel()
        self._restart_task = None
        self._next_retry_at = None
        self._backoff = None

    async def _stop_locked(self, reason: str) -> None:
        run = self._run
        if run is None:
            return
        run.stop_reason = reason
        self._set("stopping")
        await self._terminate(run)
        await self._finish_run(run)

    async def _terminate(self, run: _Run) -> None:
        proc = run.process
        if proc.returncode is not None:
            return
        self._signal(proc, signal.SIGINT)
        if await self._wait_exit(proc, self.stop_timeout_s):
            return
        log.info("engine did not stop within %.0f s; sending a second SIGINT", self.stop_timeout_s)
        self._signal(proc, signal.SIGINT)
        if await self._wait_exit(proc, self.second_sigint_s):
            return
        log.warning("engine did not stop; killing it (unflushed cache restore points are lost)")
        self._signal(proc, signal.SIGKILL)
        await self._wait_exit(proc, 10.0)

    @staticmethod
    def _signal(proc: asyncio.subprocess.Process, number: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, number)

    @staticmethod
    async def _wait_exit(proc: asyncio.subprocess.Process, timeout: float) -> bool:
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            return False
        return True

    # Spawning --------------------------------------------------------------------------

    def _internal_port(self) -> int:
        configured = self.app.settings.current.global_.engine.internal_port
        if configured != "auto":
            return int(configured)
        return free_port()

    async def _spawn(self, model: str, *, auto_restart: bool = False) -> None:
        engine = self.app.engine_cached()
        assert engine.cli is not None
        key = generate_internal_key()
        port = self._internal_port()
        try:
            spec = build_launch_for_model(
                self.app.settings,
                self.app.secrets,
                model,
                cli=engine.cli,
                internal_port=port,
                internal_key=key,
            )
        except (LaunchError, ValueError) as error:
            self.model = model
            self.error = EngineError(kind="other", code="invalid_settings", message=str(error))
            self._set("failed")
            raise ApiError(422, str(error), "invalid_settings") from None
        for directory in spec.directories:
            ensure_private_dir(directory)
        self.model = model
        self.error = None
        self.notices = []
        self.log_tail.clear()
        self.chat_template_mode = None
        self.max_context = None
        self.vision = None
        self.status = None
        self.status_at = None
        self.transport = None
        self.schema_version = None
        self.engine_models = []
        self.taken_back = None
        self._last_build_id = None
        self._last_submitted = None
        self.started_at = now_iso()
        self.ready_at = None
        self._ready_mono = None
        session_id: int | None = None
        try:
            serve = effective_serve(self.app.settings.current, model)
            session_id = self.app.usage.start_session(
                model, {"serve": serve.__dict__, "command": spec.display()}
            )
        except Exception:
            log.exception("could not record the engine session")
        await self._remove_stale_partials(model, spec, "start")
        self._log.write(f"{SESSION_START} · {model} · {spec.display()}", "stdout")
        try:
            process = await asyncio.create_subprocess_exec(
                *spec.argv,
                env=dict(spec.env),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=1024 * 1024,
            )
        except OSError as error:
            self.error = EngineError(
                kind="engine_missing", code="spawn_failed", message=f"cannot start Splash: {error}"
            )
            self._set("failed")
            if session_id is not None:
                self.app.usage.stop_session(session_id, "spawn_failed")
            raise ApiError(503, str(error), "engine_not_found") from None
        run = _Run(
            process=process,
            model=model,
            port=port,
            key=key,
            spec=spec,
            session_id=session_id,
            started_mono=time.monotonic(),
            auto_restart=auto_restart,
        )
        self._run = run
        with contextlib.suppress(OSError):
            write_atomic(self.app.paths.engine_pid, f"{process.pid}\n".encode())
        log.info("engine starting: %s (pid %s)", model, process.pid)
        self._set("starting", phase="installing")
        assert process.stdout is not None and process.stderr is not None
        run.tasks = [
            asyncio.create_task(self._read(run, process.stdout, "stdout")),
            asyncio.create_task(self._read(run, process.stderr, "stderr")),
            asyncio.create_task(self._watch(run)),
            asyncio.create_task(self._poll_ready(run)),
        ]

    async def _read(self, run: _Run, stream: asyncio.StreamReader, name: str) -> None:
        try:
            while True:
                try:
                    raw = await stream.readline()
                except ValueError:  # a line beyond the limit: take what is there
                    raw = await stream.read(1024 * 1024)
                if not raw:
                    break
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                self._on_line(run, text, name)
        finally:
            if name == "stderr":
                for event in run.parser.finish():
                    self._apply(run, event)

    def _on_line(self, run: _Run, text: str, stream: str) -> None:
        self._log.write(text, stream)
        if run is not self._run:
            return
        self.log_tail.append(text)
        for event in run.parser.feed(text):
            self._apply(run, event)
        if self.state == "starting" and self.phase == "installing":
            fetch = parse_fetching(text)
            if fetch is not None:
                self._track_install(run, fetch)
        if self.state in ("starting", "failed"):
            self._publish()

    def _apply(self, run: _Run, event: Event) -> None:
        if run is not self._run:
            return
        if isinstance(event, PhaseEvent):
            if self.state == "starting":
                order = {"installing": 0, "loading": 1, "warming": 2}
                if order[event.phase] >= order.get(self.phase or "installing", 0):
                    self._set("starting", phase=event.phase)
        elif isinstance(event, ReadyEvent):
            run.ready_line = True
            if event.context_tokens:
                self.max_context = event.context_tokens
            self.vision = not event.language_only
            self._kick_ready(run)
        elif isinstance(event, TemplateEvent):
            self.chat_template_mode = event.mode
            self._publish()
        elif isinstance(event, ErrorEvent):
            # The first specific error wins: Splash follows a budget refusal with a
            # generic "Error · native protocol reached EOF".
            if self.state == "starting" and (
                self.error is None or (self.error.kind == "other" and event.error.kind != "other")
            ):
                self.error = event.error
                self._publish(force=True)
        elif isinstance(event, NoticeEvent):
            self._add_notice(event.kind, event.message, event.message)
        elif isinstance(event, TransportEvent):
            if event.kind == "failed" and self.state in SERVING_STATES:
                self.transport = TransportInfo(recovering=True, stopped=False, error=event.message)
                self._set("recovering")
            elif event.kind == "stopped":
                self.transport = TransportInfo(recovering=False, stopped=True, error=event.message)
                self._set("engine_failed")
            elif event.kind == "restarted" and self.state == "recovering":
                self.transport = TransportInfo()
                self._set("ready")
        elif isinstance(event, WeightsEvent):
            if event.kind == "released" and self.state in ("ready", "busy"):
                self._set("idle_released")
            elif event.kind == "restored":
                self._add_notice(
                    "weights_restored",
                    f"Weights restored in {event.seconds:.2f} s"
                    if event.seconds is not None
                    else "Weights restored",
                )
                if self.state == "idle_released":
                    self._set("ready")
        elif isinstance(event, TakenBackEvent):
            self.taken_back = {
                "states": event.states,
                "kv_blocks": event.kv_blocks,
                "bytes": event.mib * 1024 * 1024,
                "left_behind": event.left_behind,
            }

    # Install progress (§6.3 starting.installing) -----------------------------------------

    def _models_dir(self, spec: LaunchSpec) -> Path:
        cache = dict(spec.env).get("HF_HUB_CACHE")
        return Path(cache) if cache else self.app.settings.models_dir()

    def _track_install(self, run: _Run, fetch: FetchLine) -> None:
        """One tracker per `Fetching` line: Splash fetches the draft, then the target,
        one repository at a time (install/upstream.py `_install`: the draft at :428,
        the target at :435)."""
        try:
            run.install = InstallTracker(
                self._models_dir(run.spec),
                fetch,
                excluded=self._downloader_digests,
                line_wall=time.time(),
            )
            run.install.sample()
        except OSError:
            log.exception("could not measure the install of %s", fetch.repo)
            run.install = None
            return
        log.info(
            "engine installing: %s files, %s bytes from %s@%s",
            fetch.files,
            fetch.total_bytes,
            fetch.repo,
            fetch.revision,
        )
        if not any(t.get_name() == "engine-install" for t in run.tasks if not t.done()):
            run.tasks.append(asyncio.create_task(self._sample_install(run), name="engine-install"))
        self._publish(force=True)

    def _downloader_digests(self) -> set[str]:
        """Blobs an unfinished Downloader item is fetching (not this install's)."""
        downloads = getattr(self.app, "downloads", None)
        if downloads is None:
            return set()
        return set(downloads.active_digests())

    async def _sample_install(self, run: _Run) -> None:
        while run is self._run and self.state == "starting" and self.phase == "installing":
            await asyncio.sleep(self.install_sample_s)
            tracker = run.install
            if tracker is None or run is not self._run or self.phase != "installing":
                continue
            try:
                await asyncio.to_thread(tracker.sample)
            except OSError:
                continue
            self._publish(force=True)

    # Stale partials (§9.4, Q24) ----------------------------------------------------------

    def _model_repos(self, model: str) -> list[str]:
        """The Hub repositories a model's files come from: the target (with its vision
        tower or projector, install/upstream.py:159-161, :204-210) and the draft."""
        repos: list[str] = []
        with contextlib.suppress(ValueError):
            repos.append(p.split_model_id(model)[0])
        draft = None
        with contextlib.suppress(Exception):
            info = self.app.model_info(model)
            ref = info.get("draft") if info else None
            draft = ref.get("repo_id") if isinstance(ref, dict) else ref
        if not draft:
            with contextlib.suppress(Exception):
                draft = effective_serve(self.app.settings.current, model).draft_model
        if isinstance(draft, str) and draft and not Path(draft).is_absolute():
            repos.append(draft)
        # A model Splash is installing for the first time has no recorded draft yet.
        family = cat.family_guess(repos[0]) if repos else None
        if family in cat.DRAFT_REPOS:
            repos.append(cat.DRAFT_REPOS[family])
        return list(dict.fromkeys(repos))

    async def _remove_stale_partials(self, model: str, spec: LaunchSpec, when: str) -> None:
        protected: set[str] = set()
        with contextlib.suppress(Exception):
            protected = self._downloader_digests()
        try:
            removed = await asyncio.to_thread(
                remove_stale_partials,
                self._models_dir(spec),
                self._model_repos(model),
                protected_digests=protected,
            )
        except Exception:
            log.exception("could not remove stale partial downloads for %s", model)
            return
        if removed:
            total = sum(r.size for r in removed)
            log.info(
                "removed %d stale partial download(s), %d bytes, for %s at engine %s: %s",
                len(removed),
                total,
                model,
                when,
                ", ".join(r.path.name for r in removed),
            )

    # Readiness ---------------------------------------------------------------------------

    def _kick_ready(self, run: _Run) -> None:
        async def check() -> None:
            await asyncio.sleep(self.ready_poll_after_line_s)
            await self._check_ready(run)

        run.tasks.append(asyncio.create_task(check()))

    async def _poll_ready(self, run: _Run) -> None:
        while run is self._run and self.state == "starting":
            await self._check_ready(run)
            if self.state != "starting":
                return
            await asyncio.sleep(self.ready_poll_s)

    async def _check_ready(self, run: _Run) -> None:
        if run is not self._run or self.state != "starting" or self._client is None:
            return
        try:
            response = await self._client.get(
                run.base_url + "/ready", headers={"Authorization": f"Bearer {run.key}"}, timeout=2.0
            )
        except httpx.HTTPError:
            return
        if response.status_code != 200:
            return
        run.ready_polls_ok += 1
        # Both the Ready line and /ready = 200 (SPEC §6.3); /ready alone for three polls
        # also counts, in case a later engine words its Ready line differently.
        if run.ready_line or run.ready_polls_ok >= 3:
            await self._became_ready(run)

    async def _became_ready(self, run: _Run) -> None:
        if run is not self._run or self.state != "starting":
            return
        await self._refresh_models(run)
        await self._poll_status(run)
        self.ready_at = now_iso()
        self._ready_mono = time.monotonic()
        self.last_request_mono = time.monotonic()
        self.error = None
        with contextlib.suppress(Exception):
            self.app.usage.set_model_facts(
                run.model,
                max_context=self.max_context,
                chat_template_mode=self.chat_template_mode,
                vision=self.vision,
                identity=(self.status or {}).get("identity"),
            )
        log.info("engine ready: %s", run.model)
        # The crash-loop alert's condition is "the engine is down after repeated
        # crashes"; a start that reaches ready clears it (docs/api.md `alert.cleared`).
        self.app.alerts.clear_condition("crash_loop")
        self._set("ready")

    async def _refresh_models(self, run: _Run) -> None:
        if self._client is None:
            return
        try:
            response = await self._client.get(
                run.base_url + "/v1/models", headers={"Authorization": f"Bearer {run.key}"}
            )
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return
        entries = data.get("data") if isinstance(data, dict) else None
        if isinstance(entries, list):
            self.engine_models = [e for e in entries if isinstance(e, dict)]
            first = self.engine_models[0] if self.engine_models else {}
            context = first.get("context_length")
            if isinstance(context, int) and context > 0:
                self.max_context = context
            if isinstance(first.get("vision"), bool):
                self.vision = first["vision"]

    # Exit handling ---------------------------------------------------------------------------

    async def _watch(self, run: _Run) -> None:
        code = await run.process.wait()
        run.exit_code = code
        # Let the readers drain what the process wrote last.
        readers = list(run.tasks[:2])
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), 5.0)
        run.readers_done.set()
        if run.stop_reason is not None or run is not self._run:
            return
        async with self._op:
            if run is not self._run:
                return
            await self._finish_run(run, crashed=True)

    async def _finish_run(self, run: _Run, *, crashed: bool = False) -> None:
        """Common tail of a stop and an unexpected exit."""
        with contextlib.suppress(Exception):
            await asyncio.wait_for(run.readers_done.wait(), 6.0)
        code = run.process.returncode
        reason = run.stop_reason or ("crash" if crashed else "exit")
        self._log.write(f"{SESSION_END} · {reason} · exit {code}", "stdout")
        if run.session_id is not None:
            with contextlib.suppress(Exception):
                self.app.usage.stop_session(run.session_id, reason)
        for task in run.tasks:
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        with contextlib.suppress(OSError):
            self.app.paths.engine_pid.unlink()
        was_ready = self._ready_mono is not None
        run.install = None
        self._run = None
        self.status = None
        self.in_flight = 0
        # A run stopped or killed mid-download leaves `<etag>.<uuid8>.incomplete`
        # files nothing can continue (huggingface_hub 1.28, Q24).
        await self._remove_stale_partials(run.model, run.spec, "exit")
        if not crashed:
            self.transport = None
            self._set("stopped")
            return
        self._on_crash(run, code, was_ready)

    def _on_crash(self, run: _Run, code: int | None, was_ready: bool) -> None:
        # D45: a crash while an auto-restart is starting is still a crash, so it
        # counts toward the crash loop (3 in 5 min → failed + alert + notification).
        if not was_ready and not run.auto_restart:
            # Startup failed: an unfixable error (SPEC §6.3 failed), never a restart loop.
            error = self.error or EngineError(
                kind="other",
                code="startup_failed",
                message=f"Splash exited during startup (exit {code})",
                raw=list(self.log_tail)[-10:],
                suggestions=[
                    EngineSuggestion(action="open_logs", label="Open logs"),
                    EngineSuggestion(action="retry", label="Retry"),
                ],
            )
            if not error.raw:
                error = error.model_copy(update={"raw": list(self.log_tail)[-10:]})
            self.error = error
            self._set("failed")
            return
        now = time.monotonic()
        stable = self._ready_mono is not None and now - self._ready_mono >= self.crash_window_s
        if stable:
            self._attempt = 0
        self._crashes.append(now)
        self._attempt += 1
        crashes = self._crashes_in_window()
        settings = self.app.settings.current.global_.lifecycle
        message = f"Splash exited unexpectedly (exit {code})"
        if len(crashes) >= self.crash_limit:
            self.error = EngineError(
                kind="crash_loop",
                code="crash_loop",
                message=f"Splash crashed {len(crashes)}× in {int(self.crash_window_s / 60)} min",
                raw=list(self.log_tail)[-10:],
                suggestions=[
                    EngineSuggestion(action="open_logs", label="Open logs"),
                    EngineSuggestion(action="restart_engine", label="Restart engine"),
                ],
            )
            self._set("failed")
            self.app.alerts.raise_alert(
                "crash_loop",
                f"Splash crashed {len(crashes)}× in {int(self.crash_window_s / 60)} min",
                self.error.message,
                source="supervisor",
                actions=[
                    NotificationAction(id="logs", label="Logs", method="GET", path="/admin/logs"),
                    NotificationAction(
                        id="restart", label="Restart", path="/api/admin/engine/restart"
                    ),
                ],
            )
            return
        if not settings.auto_restart or self._shutting_down:
            self.error = EngineError(kind="other", code="crashed", message=message)
            self._set("failed")
            return
        delay = self.backoff_s[min(self._attempt - 1, len(self.backoff_s) - 1)]
        self._backoff = delay
        self._next_retry_at = datetime.fromtimestamp(time.time() + delay, UTC).isoformat()
        self.error = EngineError(kind="other", code="crashed", message=message)
        self._set("crashed")
        model = run.model
        self._restart_task = asyncio.create_task(self._restart_after(model, delay))

    def _crashes_in_window(self) -> list[float]:
        now = time.monotonic()
        while self._crashes and now - self._crashes[0] > self.crash_window_s:
            self._crashes.popleft()
        return list(self._crashes)

    async def _restart_after(self, model: str, delay: float) -> None:
        await asyncio.sleep(delay)
        async with self._op:
            if self.state != "crashed" or self._run is not None or self._holds:
                return
            self._next_retry_at = None
            self._backoff = None
            try:
                await self._spawn(model, auto_restart=True)
            except ApiError as error:
                log.error("auto-restart failed: %s", error.message)

    # /status polling -----------------------------------------------------------------------

    def watching(self) -> bool:
        return self.app.events.subscriber_count() > 0 or self.app.watchers > 0

    async def _status_loop(self) -> None:
        while True:
            run = self._run
            interval = self.status_fast_s if self.watching() else self.status_slow_s
            if run is not None and self.state in (SERVING_STATES | {"recovering", "engine_failed"}):
                await self._poll_status(run)
            await asyncio.sleep(interval)

    async def poll_now(self) -> dict[str, Any] | None:
        run = self._run
        if run is not None:
            await self._poll_status(run)
        return self.status

    async def _poll_status(self, run: _Run) -> None:
        if self._client is None:
            return
        try:
            response = await self._client.get(
                run.base_url + "/status", headers={"Authorization": f"Bearer {run.key}"}
            )
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return
        if run is not self._run or not isinstance(data, dict):
            return
        restarted = self._detect_restart(data)
        self.status = data
        self.status_at = time.time()
        schema = data.get("schema_version")
        self.schema_version = schema if isinstance(schema, int) else None
        if (
            self.schema_version is not None
            and self.schema_version != st.SCHEMA_VERSION
            and not any(n.kind == "schema_unrecognized" for n in self.notices)
        ):
            self._add_notice(
                "schema_unrecognized",
                f"Splash status schema {self.schema_version} not recognized — some panels "
                "may be empty",
            )
        context = st.integer(data, "maximum_context_tokens")
        if context:
            self.max_context = context
        vision = st.boolean(data, "vision")
        if vision is not None:
            self.vision = vision
        later = st.text(data, "chat_template.later_system")
        if later in ("native", "patched", "unsupported"):
            self.chat_template_mode = later
        taken = st.dig(data, "disk.taken_back")
        if isinstance(taken, dict):
            self.taken_back = {k: int(v) for k, v in taken.items() if isinstance(v, int)}
        identity = data.get("identity")
        if isinstance(identity, dict) and identity != run.saved_identity and self.ready_at:
            # Per-model fingerprints for the model detail view (SPEC §10.4 Info).
            run.saved_identity = identity
            with contextlib.suppress(Exception):
                self.app.usage.set_model_facts(
                    run.model,
                    identity=identity,
                    max_context=self.max_context,
                    vision=self.vision,
                    chat_template_mode=self.chat_template_mode,
                )
        self._apply_transport(data)
        self._apply_busy(data)
        self._apply_weights(data)
        for listener in list(self.status_listeners):
            try:
                listener(data, restarted)
            except Exception:
                log.exception("status listener failed")

    def _detect_restart(self, data: dict[str, Any]) -> bool:
        build = st.build_id(data)
        submitted = st.integer(data, "requests.submitted")
        restarted = False
        if self._last_build_id is not None and build is not None and build != self._last_build_id:
            restarted = True
        if (
            self._last_submitted is not None
            and submitted is not None
            and submitted < self._last_submitted
        ):
            restarted = True
        self._last_build_id = build or self._last_build_id
        self._last_submitted = submitted if submitted is not None else self._last_submitted
        if restarted:
            self.restarts_detected += 1
        return restarted

    def _apply_transport(self, data: dict[str, Any]) -> None:
        recovering = st.boolean(data, "transport.recovering") or False
        stopped = st.boolean(data, "transport.stopped") or False
        error = st.text(data, "transport.error")
        previous = self.transport
        self.transport = TransportInfo(recovering=recovering, stopped=stopped, error=error)
        if stopped and self.state != "engine_failed":
            self._set("engine_failed")
        elif recovering and not stopped and self.state in SERVING_STATES:
            self._set("recovering")
        elif not recovering and not stopped and self.state in ("recovering", "engine_failed"):
            self._set("ready")
        elif previous != self.transport:
            self._publish()

    def _apply_busy(self, data: dict[str, Any]) -> None:
        if self.state == "ready" and (st.busy(data) or self.in_flight > 0):
            self._set("busy")
        elif self.state == "busy" and not st.busy(data) and self.in_flight == 0:
            self._set("ready")

    def _apply_weights(self, data: dict[str, Any]) -> None:
        """`weights.released` (Status.cpp appendWeights): the engine freed its weights
        after --idle-release without requests; the next request restores them."""
        released = st.boolean(data, "weights.released")
        if released and self.state == "ready" and self.in_flight == 0:
            self._set("idle_released")
        elif released is False and self.state == "idle_released":
            self._set("ready")

    # Requests from the proxy (busy, idle) ----------------------------------------------------

    def request_started(self) -> None:
        self.in_flight += 1
        self.last_request_mono = time.monotonic()
        if self.state == "ready":
            self._set("busy")
        elif self.state == "idle_released":
            # The engine restores weights on this request ("Weights restored in N s").
            self._set("busy")

    def request_finished(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)
        self.last_request_mono = time.monotonic()
        if (
            self.in_flight == 0
            and self.state == "busy"
            and (self.status is None or not st.busy(self.status))
        ):
            self._set("ready")

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(self.idle_check_s)
            with contextlib.suppress(Exception):
                await self.check_idle()

    async def check_idle(self) -> None:
        if self._run is None or self.in_flight > 0:
            return
        idle = time.monotonic() - self.last_request_mono
        lifecycle = self.app.settings.current.global_.lifecycle
        if (
            lifecycle.idle_unload
            and self.state in SERVING_STATES
            and idle >= lifecycle.idle_unload_minutes * 60
        ):
            log.info("idle unload after %.0f s without requests", idle)
            await self.stop(reason="idle_unload")
            return
        reported = (
            self.status is not None and st.boolean(self.status, "weights.released") is not None
        )
        if self.state == "ready" and not reported and idle >= self.splash_idle_release_s:
            self._set("idle_released")

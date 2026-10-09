"""Derived live metrics, the 60-minute ring buffer, minute rollups and the
`/status`-driven alerts.

Each `/status` poll (1 s while someone watches, 5 s otherwise) becomes one
`LiveMetrics` sample; rates are deltas against the previous poll and are null
for the sample after Splash restarted (a counter went down, or the identity
build id changed).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..engine import status as st
from ..events.alerts import RESTART_ACTION
from ..schemas import (
    CacheMetrics,
    DiskMetrics,
    DraftMetrics,
    KvMetrics,
    LatencyMetrics,
    LiveMetrics,
    MemoryMetrics,
    MetricsSeries,
    SchedulerMetrics,
    StageLatency,
    ThroughputMetrics,
    TotalsMetrics,
)
from ..usage.db import iso

if TYPE_CHECKING:
    from ..state import ManagerState

log = logging.getLogger(__name__)
WINDOW_S = 3600
SAMPLE_INTERVAL_S = 1.0
_TOTAL_KEYS = (
    "prompt_tokens",
    "decode_tokens",
    "total_tokens",
    "reused_tokens",
    "requests_submitted",
    "requests_completed",
    "requests_failed",
    "requests_cancelled",
)
_DISK_FAILURES = ("disk.kv_demotion_failures", "disk.kv_copy_failures", "state.offload_failures")


def _rate(cur: float | None, prev: float | None, seconds: float) -> float | None:
    if cur is None or prev is None or seconds <= 0 or cur < prev:
        return None
    return (cur - prev) / seconds


def flatten(sample: LiveMetrics) -> dict[str, float | None]:
    """Every numeric leaf as a dotted key."""
    out: dict[str, float | None] = {}
    data = sample.model_dump()
    for group in ("throughput", "totals", "cache", "draft", "latency", "scheduler", "memory", "kv"):
        for key, value in (data.get(group) or {}).items():
            if key in ("system_pressure", "growth_allowed"):
                continue
            out[f"{group}.{key}"] = _number(value)
    disk = data.get("disk") or {}
    for key in ("read_bps", "written_bps", "used_bytes", "capacity_bytes"):
        out[f"disk.{key}"] = _number(disk.get(key))
    for stage, values in (data.get("stages") or {}).items():
        out[f"stages.{stage}.p50_ms"] = _number(values.get("p50_ms"))
        out[f"stages.{stage}.p95_ms"] = _number(values.get("p95_ms"))
    return out


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


class MetricsHub:
    def __init__(self, state: ManagerState) -> None:
        self.state = state
        self.samples: deque[LiveMetrics] = deque(maxlen=WINDOW_S)
        self.latest: LiveMetrics | None = None
        self._prev: dict[str, Any] | None = None
        self._prev_t: float | None = None
        self._baseline: dict[str, int] = {}
        self._baseline_engine_start: str | None = None
        self._subscribers: set[asyncio.Queue[LiveMetrics]] = set()
        self._tasks: list[asyncio.Task[Any]] = []
        self._failure_counts: dict[str, float] = {}
        self._refused: float | None = None
        self._pressure: str | None = None

    async def start(self) -> None:
        sup = self.state.supervisor
        if sup is not None:
            sup.status_listeners.append(self.on_status)
            sup.state_listeners.append(self.on_state)
        self._tasks.append(asyncio.create_task(self._idle_sampler(), name="metrics-idle"))
        self._tasks.append(asyncio.create_task(self._rollup_loop(), name="metrics-rollup"))

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task

    # Samples --------------------------------------------------------------------

    def _engine(self) -> tuple[str, str | None]:
        sup = self.state.supervisor
        if sup is None:
            return "stopped", None
        return sup.state, sup.model

    def on_status(self, status: dict[str, Any] | None, restarted: bool) -> None:
        if status is None:
            return
        now = time.time()
        sample = self.derive(status, now, restarted)
        self._prev, self._prev_t = status, now
        self._push(sample)
        self._alerts(status, restarted)

    def on_state(self, previous: str, current: str) -> None:
        if current in ("stopped", "failed", "crashed"):
            self._prev, self._prev_t = None, None
            self._failure_counts.clear()
            self._refused = None
            for condition in (
                "engine_recovering",
                "engine_failed",
                "memory_critical",
                "memory_warning",
            ):
                self.state.alerts.clear_condition(condition)
        if current == "starting":
            self._baseline = {}

    def derive(self, status: dict[str, Any], now: float, restarted: bool = False) -> LiveMetrics:
        state, model = self._engine()
        prev, prev_t = self._prev, self._prev_t
        if restarted:
            prev = None
            self._baseline = {}
        dt = (now - prev_t) if prev_t is not None else 0.0

        def delta(path: str) -> float | None:
            if prev is None:
                return None
            return _rate(st.num(status, path), st.num(prev, path), dt)

        decode_out = st.num(status, "metrics.decode_output_tokens")
        cycle = st.num(status, "metrics.decode_cycle_ms")
        decode_cycle = None
        if prev is not None and decode_out is not None and cycle is not None:
            d_tokens = decode_out - (st.num(prev, "metrics.decode_output_tokens") or 0)
            d_ms = cycle - (st.num(prev, "metrics.decode_cycle_ms") or 0)
            if d_tokens >= 0 and d_ms > 0:
                decode_cycle = d_tokens / d_ms * 1000
        prompt = st.integer(status, "metrics.prefill_input_tokens")
        decode = st.integer(status, "metrics.decode_output_tokens")
        reused = st.integer(status, "cache.reused_tokens")
        totals_raw: dict[str, int | None] = {
            "prompt_tokens": prompt,
            "decode_tokens": decode,
            "total_tokens": (prompt or 0) + (decode or 0)
            if prompt is not None or decode is not None
            else None,
            "reused_tokens": reused,
            "requests_submitted": st.integer(status, "requests.submitted"),
            "requests_completed": st.integer(status, "requests.completed"),
            "requests_failed": st.integer(status, "requests.failed"),
            "requests_cancelled": st.integer(status, "requests.cancelled"),
        }
        totals = {
            key: (None if value is None else max(0, value - self._baseline.get(key, 0)))
            for key, value in totals_raw.items()
        }
        efficiency = None
        if reused is not None and prompt is not None and reused + prompt > 0:
            efficiency = reused / (reused + prompt)
        stages = {}
        latency = status.get("latency")
        if isinstance(latency, dict):
            for stage, hist in latency.items():
                if not isinstance(hist, dict):
                    continue
                p50 = st.histogram_percentile(hist, 0.5)
                p95 = st.histogram_percentile(hist, 0.95)
                count = hist.get("count")
                stages[stage] = StageLatency(
                    p50_ms=None if p50 is None else p50 * 1000,
                    p95_ms=None if p95 is None else p95 * 1000,
                    count=int(count) if isinstance(count, int | float) else 0,
                )
        pressure = st.text(status, "memory_governor.system_pressure")
        return LiveMetrics(
            t=now,
            engine_state=state,  # type: ignore[arg-type]
            model=model,
            restarted=restarted,
            throughput=ThroughputMetrics(
                decode_tps=st.num(status, "metrics.decode_tokens_per_second"),
                decode_tps_cycle=decode_cycle,
                prefill_tps=st.num(status, "metrics.prefill_tokens_per_second"),
                prefill_tps_delta=delta("metrics.prefill_input_tokens"),
            ),
            totals=TotalsMetrics(**totals),
            cache=CacheMetrics(
                efficiency=efficiency,
                hit_rate=st.num(status, "cache.hit_rate"),
                hits=st.integer(status, "cache.hits"),
                cold_misses=st.integer(status, "cache.cold_misses"),
            ),
            draft=DraftMetrics(
                acceptance_rate=st.num(status, "metrics.draft_acceptance_rate"),
                drafted_tokens=st.integer(status, "metrics.drafted_tokens"),
                accepted_tokens=st.integer(status, "metrics.accepted_draft_tokens"),
            ),
            latency=LatencyMetrics(
                ttft_p50_ms=st.num(status, "metrics.ttft_ms.p50"),
                ttft_p95_ms=st.num(status, "metrics.ttft_ms.p95"),
                itl_p50_ms=st.num(status, "metrics.itl_ms.p50"),
                itl_p95_ms=st.num(status, "metrics.itl_ms.p95"),
            ),
            scheduler=SchedulerMetrics(
                decoding=st.integer(status, "scheduler.decoding"),
                prefilling=st.integer(status, "scheduler.prefilling"),
                queued=st.integer(status, "scheduler.queued"),
                waiting_resources=st.integer(status, "scheduler.waiting_resources"),
                waiting_prefix=st.integer(status, "scheduler.waiting_prefix"),
                waiting_mask=st.integer(status, "scheduler.waiting_mask"),
            ),
            memory=MemoryMetrics(
                current_bytes=st.integer(status, "memory_actual.current_bytes"),
                peak_bytes=st.integer(status, "memory_actual.peak_bytes"),
                allocated_bytes=st.integer(status, "memory_actual.allocated_bytes"),
                limit_bytes=st.integer(status, "memory_governor.limit_bytes"),
                charged_bytes=st.integer(status, "memory_governor.charged_bytes"),
                headroom_bytes=st.integer(status, "memory_governor.headroom_bytes"),
                host_available_bytes=st.integer(status, "memory_governor.host_available_bytes"),
                host_reserve_bytes=st.integer(status, "memory_governor.host_reserve_bytes"),
                system_pressure=pressure if pressure in ("normal", "warning", "critical") else None,  # type: ignore[arg-type]
                growth_allowed=st.boolean(status, "memory_governor.growth_allowed"),
            ),
            kv=KvMetrics(
                pages_active=st.integer(status, "kv.pages_active"),
                pages_cache=st.integer(status, "kv.pages_cache"),
                pages_free=st.integer(status, "kv.pages_free"),
                pages_allocated=st.integer(status, "kv.pages_allocated"),
            ),
            disk=DiskMetrics(
                enabled=(st.num(status, "disk.capacity_bytes") or 0) > 0,
                persistent=bool(st.boolean(status, "disk.persistent")),
                read_bps=delta("disk.read_bytes"),
                written_bps=delta("disk.written_bytes"),
                used_bytes=st.integer(status, "disk.used_bytes"),
                capacity_bytes=st.integer(status, "disk.capacity_bytes"),
            ),
            stages=stages,
        )

    def stopped_sample(self, now: float | None = None) -> LiveMetrics:
        state, model = self._engine()
        return LiveMetrics(t=now or time.time(), engine_state=state, model=model)  # type: ignore[arg-type]

    def _push(self, sample: LiveMetrics) -> None:
        self.latest = sample
        self.samples.append(sample)
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(sample)

    async def _idle_sampler(self) -> None:
        """While the engine isn't serving, one engine-state sample per second, so the
        charts show the gap and the stream keeps `engine_state` current."""
        while True:
            await asyncio.sleep(SAMPLE_INTERVAL_S)
            sup = self.state.supervisor
            serving = sup is not None and sup.state in (
                "ready",
                "busy",
                "idle_released",
                "recovering",
                "engine_failed",
            )
            if not serving:
                self._push(self.stopped_sample())

    def reset(self) -> None:
        """Clear the GUI's "since engine start" baseline; Splash's own
        counters are untouched."""
        status = self.state.supervisor.status if self.state.supervisor else None
        if status is None:
            self._baseline = {}
            return
        latest = self.derive(status, time.time())
        base = latest.totals.model_dump()
        self._baseline = {
            k: int(v) + self._baseline.get(k, 0) for k, v in base.items() if isinstance(v, int)
        }

    def current(self) -> LiveMetrics:
        sup = self.state.supervisor
        if self.latest is not None and sup is not None and self.latest.engine_state == sup.state:
            return self.latest
        if sup is not None and sup.status is not None:
            return self.derive(sup.status, time.time())
        return self.stopped_sample()

    async def stream(self) -> AsyncIterator[tuple[str, Any]]:
        queue: asyncio.Queue[LiveMetrics] = asyncio.Queue(120)
        self._subscribers.add(queue)
        self.state.watchers += 1
        try:
            yield "snapshot", self.current()
            while True:
                yield "snapshot", await queue.get()
        finally:
            self._subscribers.discard(queue)
            self.state.watchers = max(0, self.state.watchers - 1)

    def series(self, window: int) -> MetricsSeries:
        now = int(time.time())
        start = now - window + 1
        by_second: dict[int, dict[str, float | None]] = {}
        for sample in self.samples:
            second = int(sample.t)
            if second >= start:
                by_second[second] = flatten(sample)
        keys: set[str] = set()
        for values in by_second.values():
            keys.update(values)
        seconds = list(range(start, now + 1))
        series: dict[str, list[float | None]] = {k: [] for k in sorted(keys)}
        for second in seconds:
            values = by_second.get(second, {})
            for key in series:
                series[key].append(values.get(key))
        return MetricsSeries(
            window_s=window, step_s=1, t=[float(s) for s in seconds], series=series
        )

    # Minute rollups ---------------------------------------------------

    async def _rollup_loop(self) -> None:
        while True:
            now = datetime.now(UTC)
            nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=1, seconds=1)
            await asyncio.sleep(max(1.0, (nxt - now).total_seconds()))
            with contextlib.suppress(Exception):
                self.rollup(nxt - timedelta(minutes=1, seconds=1))

    def rollup(self, minute_dt: datetime) -> None:
        minute_dt = minute_dt.astimezone(UTC).replace(second=0, microsecond=0)
        start = minute_dt.timestamp()
        end = start + 60
        per_model: dict[str, list[float]] = {}
        for sample in self.samples:
            if start <= sample.t < end and sample.model and sample.throughput.decode_tps:
                per_model.setdefault(sample.model, []).append(sample.throughput.decode_tps)
        samples = {
            model: {"decode_tps_avg": sum(v) / len(v)} for model, v in per_model.items() if v
        }
        self.state.usage.rollup_minute(iso(minute_dt), samples)

    # Alerts -------------------------------------------------------------

    def _alerts(self, status: dict[str, Any], restarted: bool) -> None:
        alerts = self.state.alerts
        recovering = st.boolean(status, "transport.recovering") or False
        stopped = st.boolean(status, "transport.stopped") or False
        error = st.text(status, "transport.error") or "the engine is restarting"
        if stopped:
            if not alerts.active("engine_failed"):
                alerts.raise_alert(
                    "engine_failed",
                    "Engine stopped after repeated failures",
                    error,
                    source="status",
                    actions=[RESTART_ACTION],
                )
        else:
            alerts.clear_condition("engine_failed")
        if recovering and not stopped:
            if not alerts.active("engine_recovering"):
                alerts.raise_alert(
                    "engine_recovering", f"Engine recovering: {error}", error, source="status"
                )
        else:
            alerts.clear_condition("engine_recovering")
        pressure = st.text(status, "memory_governor.system_pressure")
        if pressure != self._pressure:
            self._pressure = pressure
            if pressure == "critical":
                alerts.clear_condition("memory_warning")
                alerts.raise_alert(
                    "memory_critical",
                    "Critical memory pressure — long requests may be suspended",
                    "macOS reports critical memory pressure; Splash's /ready answers 503.",
                    source="status",
                )
            elif pressure == "warning":
                alerts.clear_condition("memory_critical")
                alerts.raise_alert(
                    "memory_warning",
                    "Memory pressure is high",
                    "macOS reports memory pressure; close memory-heavy apps.",
                    source="status",
                    notify=False,
                )
            else:
                alerts.clear_condition("memory_critical")
                alerts.clear_condition("memory_warning")
        elif pressure in ("normal", "warning"):
            # Every poll, not only a transition: /status is the source of memory_critical
            # , so a poll that no longer reports critical pressure clears it.
            alerts.clear_condition("memory_critical")
        if restarted:
            self._failure_counts.clear()
            self._refused = None
        failures = {path: st.num(status, path) for path in _DISK_FAILURES}
        grew = any(
            value is not None
            and path in self._failure_counts
            and value > self._failure_counts[path]
            for path, value in failures.items()
        )
        for path, value in failures.items():
            if value is not None:
                self._failure_counts[path] = value
        if grew:
            alerts.raise_alert(
                "disk_tier_failures",
                "SSD cache write failed — writes disabled for this session",
                "Splash reported a disk tier failure (kv_demotion_failures, kv_copy_failures or "
                "offload_failures increased).",
                source="status",
            )
        refused = st.num(status, "disk.write_behind.refused")
        if refused is not None and self._refused is not None and refused > self._refused:
            alerts.raise_alert(
                "write_behind_refused",
                "Persistent cache paused: hourly write cap (128 GiB/h) reached",
                "",
                source="status",
            )
        if refused is not None:
            self._refused = refused
        schema = status.get("schema_version")
        if (
            isinstance(schema, int)
            and schema != st.SCHEMA_VERSION
            and not alerts.active("schema_unrecognized")
        ):
            alerts.raise_alert(
                "schema_unrecognized",
                f"Splash status schema {schema} not recognized — some panels may be empty",
                "",
                source="status",
                notify=False,
            )

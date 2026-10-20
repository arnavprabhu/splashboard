"""Derived live metrics, the ring buffer, rollups and /status alerts
(`metrics/live.py`)."""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.metrics.live import MetricsHub, flatten

from .fakeengine import MODEL, EngineHarness


def status(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 6,
        "identity": {"build_id": "b1"},
        "requests": {"submitted": 10, "completed": 9, "failed": 1, "cancelled": 0},
        "metrics": {
            "prefill_input_tokens": 1000,
            "decode_output_tokens": 500,
            "decode_cycle_ms": 5000.0,
            "decode_tokens_per_second": 74.0,
            "prefill_tokens_per_second": 900.0,
            "ttft_ms": {"p50": 120.0, "p95": 300.0},
            "itl_ms": {"p50": 13.0, "p95": 20.0},
            "drafted_tokens": 100,
            "accepted_draft_tokens": 60,
            "draft_acceptance_rate": 0.6,
        },
        "cache": {"reused_tokens": 3000, "hit_rate": 0.5, "hits": 4, "cold_misses": 4},
        "scheduler": {"decoding": 1, "prefilling": 0, "queued": 2, "waiting_resources": 0},
        "memory_actual": {"current_bytes": 10, "peak_bytes": 20, "allocated_bytes": 15},
        "memory_governor": {
            "limit_bytes": 100,
            "system_pressure": "normal",
            "growth_allowed": True,
        },
        "kv": {"pages_active": 3, "pages_cache": 4, "pages_free": 5, "pages_allocated": 12},
        "disk": {
            "capacity_bytes": 1000,
            "used_bytes": 100,
            "read_bytes": 0,
            "written_bytes": 0,
            "persistent": True,
            "kv_demotion_failures": 0,
            "kv_copy_failures": 0,
            "write_behind": {"refused": 0},
        },
        "state": {"offload_failures": 0},
        "transport": {"recovering": False, "stopped": False, "error": None},
        "latency": {"http_ttft": {"count": 0, "buckets": []}},
    }
    for path, value in over.items():
        node = base
        *parents, leaf = path.split("__")
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = value
    return base


def hub_of(app: FastAPI) -> MetricsHub:
    hub = app.state.manager.metrics
    assert isinstance(hub, MetricsHub)
    return hub


def conditions(app: FastAPI) -> set[str]:
    return {a.condition for a in app.state.manager.alerts.all()}


def test_rates_come_from_deltas_between_polls(app: FastAPI) -> None:
    hub = hub_of(app)
    first = status()
    second = status(
        metrics__prefill_input_tokens=3000,
        metrics__decode_output_tokens=700,
        metrics__decode_cycle_ms=6000.0,
        disk__read_bytes=4096,
        disk__written_bytes=2048,
    )
    a = hub.derive(first, 100.0)
    assert a.throughput.prefill_tps_delta is None, "no previous poll yet"
    hub._prev, hub._prev_t = first, 100.0
    b = hub.derive(second, 102.0)
    assert b.throughput.prefill_tps_delta == 1000.0
    assert b.throughput.decode_tps == 74.0
    assert b.throughput.decode_tps_cycle == 200.0, "200 tokens over 1000 ms of decode cycles"
    assert b.disk.read_bps == 2048.0 and b.disk.written_bps == 1024.0
    assert b.cache.efficiency == 3000 / (3000 + 3000)
    assert b.totals.total_tokens == 3700
    assert b.scheduler.queued == 2 and b.kv.pages_free == 5
    assert b.memory.system_pressure == "normal" and b.disk.enabled and b.disk.persistent


def test_a_counter_going_down_discards_the_sample(app: FastAPI) -> None:
    hub = hub_of(app)
    hub._prev, hub._prev_t = status(metrics__prefill_input_tokens=9000), 100.0
    sample = hub.derive(status(), 101.0)
    assert sample.throughput.prefill_tps_delta is None
    hub._prev, hub._prev_t = status(), 100.0
    restarted = hub.derive(status(metrics__prefill_input_tokens=5000), 101.0, restarted=True)
    assert restarted.throughput.prefill_tps_delta is None and restarted.restarted is True


def test_reset_sets_a_gui_baseline(app: FastAPI) -> None:
    hub = hub_of(app)
    sup = app.state.manager.supervisor
    sup.status = status()
    hub.reset()
    assert hub.derive(status(), time.time()).totals.prompt_tokens == 0
    later = hub.derive(status(metrics__prefill_input_tokens=1500), time.time())
    assert later.totals.prompt_tokens == 500
    assert later.totals.requests_submitted == 0


def test_series_and_flatten(app: FastAPI, client: TestClient) -> None:
    hub = hub_of(app)
    now = time.time()
    hub._push(hub.derive(status(), now - 2))
    hub._push(hub.derive(status(), now - 1))
    flat = flatten(hub.samples[-1])
    assert flat["throughput.decode_tps"] == 74.0 and "memory.system_pressure" not in flat
    body = client.get("/api/admin/metrics/series", params={"window": 5}).json()
    assert body["window_s"] == 5 and len(body["t"]) == 5
    decode = body["series"]["throughput.decode_tps"]
    assert decode.count(74.0) >= 1 and None in decode, "gaps are nulls"
    assert client.get("/api/admin/metrics/series", params={"window": 0}).status_code == 422


def test_rollup_writes_decode_average(app: FastAPI) -> None:
    hub = hub_of(app)
    minute = datetime(2026, 10, 4, 12, 30, tzinfo=UTC)
    sup = app.state.manager.supervisor
    sup.model = MODEL
    for second, tps in ((5, 60.0), (20, 80.0)):
        sample = hub.derive(
            status(metrics__decode_tokens_per_second=tps), minute.timestamp() + second
        )
        sample.model = MODEL
        hub.samples.append(sample)
    app.state.manager.usage.insert_request(
        {
            "ts": "2026-10-04T12:30:10+00:00",
            "model": MODEL,
            "endpoint": "/v1/chat/completions",
            "status": 200,
            "prompt_tokens": 5,
            "ttft_ms": 50.0,
        }
    )
    hub.rollup(minute)
    rows = app.state.manager.usage.rollups("2026-10-04T12:30:00", "2026-10-04T12:31:00")
    assert len(rows) == 1
    assert (
        rows[0]["decode_tps_avg"] == 70.0
        and rows[0]["requests"] == 1
        and rows[0]["ttft_p50"] == 50.0
    )


def test_status_alerts_raise_and_clear(app: FastAPI) -> None:
    hub = hub_of(app)
    hub.on_status(status(transport__recovering=True, transport__error="metal reset"), False)
    assert "engine_recovering" in conditions(app)
    alert = app.state.manager.alerts.get("engine_recovering")
    assert "metal reset" in alert.title
    hub.on_status(status(transport__stopped=True), False)
    assert "engine_failed" in conditions(app) and "engine_recovering" not in conditions(app)
    hub.on_status(status(), False)
    assert not {"engine_failed", "engine_recovering"} & conditions(app)
    hub.on_status(status(memory_governor__system_pressure="warning"), False)
    assert "memory_warning" in conditions(app)
    hub.on_status(status(memory_governor__system_pressure="critical"), False)
    assert "memory_critical" in conditions(app) and "memory_warning" not in conditions(app)
    hub.on_status(status(), False)
    assert not {"memory_critical", "memory_warning"} & conditions(app)


def test_memory_critical_clears_on_a_poll_that_is_not_critical(app: FastAPI) -> None:
    """memory_critical is driven by /status. A raise from another source,
    such as the old /ready 503 path, must not outlive the next poll that says the
    pressure is normal, even though pressure never changed."""
    hub = hub_of(app)
    hub.on_status(status(), False)
    app.state.manager.alerts.raise_alert(
        "memory_critical", "Critical memory pressure", "/ready answered 503", source="status"
    )
    hub.on_status(status(), False)
    assert "memory_critical" not in conditions(app)
    hub.on_status(status(memory_governor__system_pressure="warning"), False)
    app.state.manager.alerts.raise_alert(
        "memory_critical", "Critical memory pressure", "", source="status"
    )
    hub.on_status(status(memory_governor__system_pressure="warning"), False)
    assert "memory_critical" not in conditions(app)


def test_a_transport_recovery_is_not_memory_pressure(app: FastAPI) -> None:
    """Splash answers /ready 503 during transport recovery too (server/backend.py)."""
    hub = hub_of(app)
    hub.on_status(status(transport__recovering=True, transport__error="metal reset"), False)
    assert "engine_recovering" in conditions(app)
    assert "memory_critical" not in conditions(app)


def test_disk_failures_and_write_cap_alert_only_when_they_grow(app: FastAPI) -> None:
    hub = hub_of(app)
    hub.on_status(status(disk__kv_copy_failures=2, disk__write_behind={"refused": 1}), False)
    assert not {"disk_tier_failures", "write_behind_refused"} & conditions(app), (
        "the first poll is a baseline, not an increase"
    )
    hub.on_status(status(disk__kv_copy_failures=3, disk__write_behind={"refused": 2}), False)
    assert {"disk_tier_failures", "write_behind_refused"} <= conditions(app)
    severity = app.state.manager.alerts.get("write_behind_refused").severity
    assert severity == "info"


def test_offload_failures_count_as_disk_failures(app: FastAPI) -> None:
    hub = hub_of(app)
    hub.on_status(status(), False)
    hub.on_status(status(state__offload_failures=1), False)
    assert "disk_tier_failures" in conditions(app)


def test_unknown_schema_is_a_banner(app: FastAPI) -> None:
    hub_of(app).on_status(status(schema_version=7), False)
    alert = app.state.manager.alerts.get("schema_unrecognized")
    assert alert is not None and "schema 7" in alert.title


def test_engine_stopping_clears_status_alerts(app: FastAPI) -> None:
    hub = hub_of(app)
    hub.on_status(status(transport__recovering=True), False)
    hub.on_state("ready", "stopped")
    assert "engine_recovering" not in conditions(app)


def test_tolerates_missing_groups(app: FastAPI) -> None:
    sample = hub_of(app).derive({"schema_version": 6}, time.time())
    assert sample.throughput.decode_tps is None and sample.disk.enabled is False
    assert sample.stages == {}


def test_live_samples_from_the_fake_engine(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory()
    h.load()
    h.client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 32},
    )
    snapshot = h.client.get("/api/admin/metrics/snapshot").json()
    assert snapshot["engine_state"] in ("ready", "busy") and snapshot["model"] == MODEL
    assert snapshot["totals"]["requests_completed"] >= 1
    assert snapshot["memory"]["limit_bytes"]
    assert h.client.post("/api/admin/metrics/reset").json()["ok"] is True
    after = h.client.get("/api/admin/metrics/snapshot").json()
    assert after["totals"]["requests_completed"] == 0


def test_stopped_snapshot(client: TestClient) -> None:
    snapshot = client.get("/api/admin/metrics/snapshot").json()
    assert snapshot["engine_state"] == "stopped" and snapshot["throughput"]["decode_tps"] is None


def test_copy_safety() -> None:
    # The fixture helper must not share nested dicts between calls.
    a, b = status(), status()
    a["metrics"]["decode_output_tokens"] = 1
    assert b["metrics"]["decode_output_tokens"] == 500 and copy.deepcopy(a) == a


def test_disk_limit_gate_and_budget_from_splash_1_3_2(app: FastAPI) -> None:
    """Under the automatic quota the tier can hold less than `capacity_bytes`: the live
    `limit_bytes`, the volume's free space, the write gate and the budget come through."""
    sample = hub_of(app).derive(
        status(
            disk__capacity_bytes=34_359_738_368,
            disk__limit_bytes=8_589_934_592,
            disk__volume_available_bytes=46_170_898_432,
            disk__writes_gated="budget",
            disk__write_budget_bytes=0,
        ),
        time.time(),
    )
    disk = sample.disk
    assert disk.enabled and disk.capacity_bytes == 34_359_738_368
    assert disk.limit_bytes == 8_589_934_592 and disk.volume_available_bytes == 46_170_898_432
    assert disk.writes_gated == "budget" and disk.write_budget_bytes == 0
    flat = flatten(sample)
    assert flat["disk.limit_bytes"] == 8_589_934_592.0 and "disk.writes_gated" not in flat
    unknown = hub_of(app).derive(status(disk__writes_gated="later"), time.time())
    assert unknown.disk.writes_gated is None


def test_a_cache_turned_off_is_not_enabled(app: FastAPI) -> None:
    off = hub_of(app).derive(status(disk__capacity_bytes=0, disk__limit_bytes=0), time.time())
    assert off.disk.enabled is False
    limited = hub_of(app).derive(
        status(disk__capacity_bytes=0, disk__limit_bytes=1024), time.time()
    )
    assert limited.disk.enabled is True


def test_writes_closed_raises_a_disk_alert_once(app: FastAPI) -> None:
    hub = hub_of(app)
    hub.on_status(status(disk__writes_gated="no"), False)
    assert "disk_tier_failures" not in conditions(app)
    hub.on_status(status(disk__writes_gated="closed"), False)
    alert = app.state.manager.alerts.get("disk_tier_failures")
    assert alert is not None and "stopped taking writes" in alert.title


def test_writes_closed_on_the_first_poll_after_a_restart_alerts(app: FastAPI) -> None:
    hub = hub_of(app)
    hub.on_status(status(disk__writes_gated="closed"), True)
    alert = app.state.manager.alerts.get("disk_tier_failures")
    assert alert is not None and "stopped taking writes" in alert.title


def test_write_budget_spent_raises_an_info_banner_until_it_refills(app: FastAPI) -> None:
    hub = hub_of(app)
    hub.on_status(status(disk__writes_gated="no"), False)
    assert "disk_write_budget" not in conditions(app)
    hub.on_status(status(disk__writes_gated="budget"), False)
    alert = app.state.manager.alerts.get("disk_write_budget")
    assert alert is not None and alert.severity == "info"
    assert "write budget" in alert.title
    hub.on_status(status(disk__writes_gated="budget"), False)
    assert app.state.manager.alerts.get("disk_write_budget").count == 1
    hub.on_status(status(disk__writes_gated="no"), False)
    assert "disk_write_budget" not in conditions(app)
    hub.on_status(status(disk__writes_gated="budget"), False)
    hub.on_state("ready", "stopped")
    assert "disk_write_budget" not in conditions(app)
    # A cache that stops taking writes clears the budget banner: it writes nothing even
    # once the budget refills, and the closed alert says so.
    hub.on_status(status(disk__writes_gated="budget"), False)
    assert "disk_write_budget" in conditions(app)
    hub.on_status(status(disk__writes_gated="closed"), False)
    assert "disk_write_budget" not in conditions(app)
    assert "disk_tier_failures" in conditions(app)


def test_active_requests_host_spare_and_deduplicated_tokens(app: FastAPI) -> None:
    sample = hub_of(app).derive(
        status(
            active_requests=[
                {
                    "id": 7,
                    "phase": "prefill",
                    "priority": "foreground",
                    "prompt_tokens": 4096,
                    "prompt_processed": 2048,
                    "generated_tokens": 0,
                    "max_new_tokens": 256,
                    "age_ms": 1500.5,
                },
                {"phase": "decode"},
                "junk",
            ],
            cache__deduplicated_kv_tokens=640,
            memory_governor__host_spare_bytes=8_589_934_592,
            memory_governor__host_spare_reserve_bytes=2_147_483_648,
            memory_governor__host_headroom_bytes=6_442_450_944,
        ),
        time.time(),
    )
    assert [r.model_dump() for r in sample.active_requests] == [
        {
            "id": 7,
            "phase": "prefill",
            "priority": "foreground",
            "prompt_tokens": 4096,
            "prompt_processed": 2048,
            "generated_tokens": 0,
            "max_new_tokens": 256,
            "age_ms": 1500.5,
            "kv_mode": None,
        }
    ], "entries without an id are skipped"
    assert sample.totals.deduplicated_kv_tokens == 640
    assert sample.memory.host_spare_bytes == 8_589_934_592
    assert sample.memory.host_spare_reserve_bytes == 2_147_483_648
    assert sample.memory.host_headroom_bytes == 6_442_450_944
    assert hub_of(app).derive(status(), time.time()).active_requests == []


def spill(**over: Any) -> dict[str, Any]:
    """A Qwen3.8-Flash-Next target's /status: its KV can spill to the SSD cache."""
    return status(
        kv_spill={
            "write_through": {"failures": 0, "paused": False},
            "block_cache": {"read_failures": 0},
        },
        **over,
    )


def test_kv_mode_and_writes_deferred(app: FastAPI) -> None:
    row = {"id": 1, "phase": "decode", "prompt_tokens": 600_000}
    sample = hub_of(app).derive(
        status(
            active_requests=[
                {**row, "kv_mode": "spill"},
                {**row, "id": 2, "kv_mode": "resident"},
                {**row, "id": 3, "kv_mode": "elsewhere"},
                {**row, "id": 4},
            ],
            disk__writes_deferred_ms=12.5,
        ),
        time.time(),
    )
    assert [r.kv_mode for r in sample.active_requests] == ["spill", "resident", None, None]
    assert sample.disk.writes_deferred_ms == 12.5


def test_spilled_kv_write_and_read_failures_are_disk_failures(app: FastAPI) -> None:
    hub = hub_of(app)
    alerts = app.state.manager.alerts
    hub.on_status(spill(), False)
    # A full volume pauses write-through until it has room: said so, then cleared.
    hub.on_status(spill(kv_spill__write_through={"failures": 1, "paused": True}), False)
    alert = alerts.get("disk_tier_failures:spill_write")
    assert alert is not None and alert.title.endswith("long requests' KV writes paused")
    hub.on_status(spill(kv_spill__write_through={"failures": 1, "paused": False}), False)
    assert alerts.get("disk_tier_failures:spill_write") is None
    # A write failure with the tier closed is off for the session.
    hub.on_status(spill(kv_spill__write_through={"failures": 2, "paused": False}), False)
    alert = alerts.get("disk_tier_failures:spill_write")
    assert alert is not None and "disabled for this session" in alert.title
    # A failed read stands beside the write failure instead of replacing its banner.
    hub.on_status(
        spill(
            kv_spill__write_through={"failures": 2, "paused": False},
            kv_spill__block_cache={"read_failures": 2},
        ),
        False,
    )
    assert alerts.get("disk_tier_failures:read").title == "SSD cache read failed"
    assert "disabled for this session" in alerts.get("disk_tier_failures:spill_write").title


def test_an_expert_load_failure_asks_for_a_restart(app: FastAPI) -> None:
    hub = hub_of(app)
    hub.on_status(status(streamed_experts={"failure": ""}), False)
    assert "expert_streaming_failed" not in conditions(app)
    failed = status(streamed_experts={"failure": "cannot read expert 3 of layer 7"})
    hub.on_status(failed, False)
    hub.on_status(failed, False)
    alert = app.state.manager.alerts.get("expert_streaming_failed")
    assert alert is not None and alert.severity == "critical" and alert.count == 1
    assert "cannot read expert 3 of layer 7" in alert.message
    assert [a.id for a in alert.actions] == ["restart"]
    # The restarted engine's store is healthy again.
    hub.on_status(status(streamed_experts={"failure": ""}), True)
    assert "expert_streaming_failed" not in conditions(app)
    hub.on_status(failed, False)
    hub.on_state("ready", "stopped")
    assert "expert_streaming_failed" not in conditions(app)


def test_a_spent_write_budget_refuses_long_requests_on_a_spill_target(app: FastAPI) -> None:
    hub = hub_of(app)
    hub.on_status(status(disk__writes_gated="budget"), False)
    alert = app.state.manager.alerts.get("disk_write_budget")
    assert alert is not None and "Long requests" not in alert.message
    hub.on_state("ready", "stopped")
    hub.on_status(spill(disk__writes_gated="budget"), False)
    alert = app.state.manager.alerts.get("disk_write_budget")
    assert alert is not None and "Long requests whose KV must go to the SSD cache" in alert.message

"""/status contract against ./splash: every key path the native engine writes
(runtime/engine/Status.cpp, as its golden status documents hold them), and the
server-side groups (server/server.py, backend.py, frontend.py), must appear in
the fake's /status at the same nesting."""

from __future__ import annotations

import json
import re

import pytest

from conftest import REAL_SPLASH
from harness import FakeSplash

ENGINE = REAL_SPLASH / "runtime" / "engine"
GOLDEN = REAL_SPLASH / "dev" / "tests" / "engine"
# Status.cpp writes the warmup step names through json::quote(name).
WARMUP_STEPS = ("prefill_2048", "decode_b1", "decode_b2", "decode_b3", "decode_b4", "composite_state_restore")
# Server-side groups, with the fields their source writes.
SERVER_GROUPS = {
    # server/server.py FrontendServer.status
    "instance": {"id", "pid", "model", "host", "port", "started_at"},
    "http": {"requests", "request_body_bytes", "max_request_bytes", "token_counts", "connections"},
    # server/backend.py NativeBackend.status (error only while stale)
    "transport": {
        "ready",
        "recovering",
        "stopped",
        "pending",
        "pending_limit",
        "restarts",
        "last_crash_trace",
        "status_stale",
        "status_age_ms",
    },
    # server/frontend.py Frontend.status and the stats() it reads
    "frontend": {"preparation_capacity", "active", "waiting"},
    "grammar_cache": {"entries", "capacity", "source_bytes", "source_budget_bytes", "hits", "misses"},
    "response_store": {"entries", "bytes", "budget_bytes", "evictions", "hits", "misses"},
    "image_cache": {"entries", "bytes", "budget_bytes", "request_bytes", "request_budget_bytes"},
    "tokenizer_cache": {"enabled", "entries", "bytes", "budget_bytes", "capacity", "hits", "reused_tokens"},
    "chat_template": {"later_system"},
}


def native_status_paths() -> set[str]:
    """Every key path of the engine's own status documents: the golden one its
    status test compares (runtime_status_test.cpp), and that test's variants, which
    hold the groups the golden one leaves null."""
    paths = flatten(json.loads((GOLDEN / "status_golden.json").read_text()))
    for line in (GOLDEN / "status_variants_golden.jsonl").read_text().splitlines():
        if line.strip():
            paths |= flatten(json.loads(line)["status"])
    return paths


def flatten(value: object, prefix: tuple[str, ...] = ()) -> set[str]:
    """Every key path of a document; a list's items share their list's path."""
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            path = (*prefix, key)
            paths.add(".".join(path))
            paths |= flatten(child, path)
    elif isinstance(value, list):
        for item in value:
            paths |= flatten(item, prefix)
    return paths


# Groups the engine writes only for some targets: null unless the store streams
# (streamed_table, streamed_experts, rope_scaling past the native window),
# kv_spill and the plan's spill floors only where a target's KV can spill, and
# active requests while in service. test_status_1_4_0.py checks them on a
# Flash-Next target.
CONDITIONAL = (
    "active_requests.",
    "streamed_table.",
    "streamed_experts.",
    "identity.cache.rope_scaling.",
    "kv_spill",
    "memory_plan.budget.kv_spill.",
    # A windowed target's (Flint's, 1.4.1); test_splash_1_4_1.py checks them.
    "memory_plan.budget.kv_window",
    "kv.window",
)


@pytest.fixture(scope="module")
def status(tmp_path_factory) -> dict:
    with FakeSplash(tmp_path_factory.mktemp("contract"), args=("--no-webui", "--max-cache-disk", "1G")) as fake:
        code, body = fake.json("GET", "/status")
    assert code == 200
    assert isinstance(body, dict)
    return body


@pytest.mark.skipif(not GOLDEN.exists(), reason="no ./splash reference clone")
def test_extractor_reads_the_engine_source():
    paths = native_status_paths()
    # Sanity: the parser found the nesting, not just names.
    for path in (
        "identity.cache.build_id",
        "memory_plan.budget.hard_budget_bytes",
        "memory_plan.model.memory.target_weights_bytes",
        "disk.taken_back.left_behind",
        "metrics.current_decode_batch.tokens_per_second",
        "scheduler.decode_batches_by_width.b4",
        "streamed_experts.decode.host_steps.work_ms.p99",
        "streamed_table.prefill.stage_ms.samples",
        "kv_spill.block_cache.read_failures",
        "kv_spill.requests.refused.write_budget",
        "memory_plan.budget.context_bounds.bound",
        "memory_plan.budget.kv_spill.floor_tokens",
        "identity.cache.rope_scaling.factor",
    ):
        assert path in paths, path
    assert len(paths) > 250


@pytest.mark.skipif(not GOLDEN.exists(), reason="no ./splash reference clone")
def test_fake_status_has_every_native_key_path(status):
    # An idle engine has no active requests and an in-memory target streams
    # nothing; test_splash_1_3_2.py and test_status_1_4_0.py check those.
    expected = {p for p in native_status_paths() if not p.startswith(CONDITIONAL)}
    missing = expected - flatten(status)
    assert not missing, sorted(missing)
    assert status["active_requests"] == []
    assert status["streamed_table"] is status["streamed_experts"] is None
    assert "kv_spill" not in status


def test_fake_status_has_server_groups(status):
    for group, fields in SERVER_GROUPS.items():
        assert group in status, group
        assert fields <= set(status[group]), (group, fields - set(status[group]))
    assert status["schema_version"] == 8
    assert status["vision"] is True and status["input_modalities"] == ["text", "image", "pdf"]
    # server/latency.py stages are histograms keyed by stage name.
    assert {"upload", "http_request", "http_ttft", "output_interval"} <= set(status["latency"])


def test_prefill_warmup_name_matches_engine_constant():
    geometry = REAL_SPLASH / "runtime" / "metal" / "abi" / "ExecutionGeometry.h"
    if not geometry.exists():
        pytest.skip("no ./splash reference clone")
    budget = re.search(r"#define SPLASH_PREFILL_TOKEN_BUDGET (\d+)u", geometry.read_text())
    assert budget is not None and f"prefill_{budget.group(1)}" == WARMUP_STEPS[0]

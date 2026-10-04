"""/status contract against ./splash: every key path the native engine writes
(runtime/engine/Status.cpp with MemoryPlan.cpp and MemoryAudit.cpp), and the
server-side groups (server/server.py, backend.py, frontend.py), must appear in
the fake's /status at the same nesting."""

from __future__ import annotations

import re

import pytest

from conftest import REAL_SPLASH
from harness import FakeSplash

ENGINE = REAL_SPLASH / "runtime" / "engine"
LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"|\'(\\.|[^\'\\])\'')
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


def _body(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", source.index(")", start))
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[brace + 1 : index]
    raise ValueError(signature)


def _literals(body: str, substitutions: dict[str, str] | None = None) -> str:
    """The text of a C++ stream's string and char literals, in order, with
    calls that write nested JSON replaced by that JSON's literal text."""
    for call, text in (substitutions or {}).items():
        body = body.replace(call, '"' + text.replace('"', '\\"') + '"')
    parts = []
    for match in LITERAL.finditer(body):
        parts.append(match.group(1).replace('\\"', '"') if match.group(1) is not None else match.group(2))
    return "".join(parts)


def _paths(stream: str) -> set[str]:
    paths: set[str] = set()
    stack: list[str] = []
    pending: str | None = None
    for match in re.finditer(r'"([a-z0-9_]+)":|\{|\}', stream):
        token = match.group(0)
        if token == "{":
            stack.append(pending or "")
            pending = None
        elif token == "}":
            if stack:
                stack.pop()
            pending = None
        else:
            pending = match.group(1)
            paths.add(".".join([*stack[1:], pending]))
    return paths


def native_status_paths() -> set[str]:
    status = (ENGINE / "Status.cpp").read_text()
    plan = (ENGINE / "MemoryPlan.cpp").read_text()
    audit = (ENGINE / "MemoryAudit.cpp").read_text()
    breakdown = _literals(_body(plan, "std::string EngineMemoryBreakdown::toStatusJson() const"))
    device = _literals(_body(plan, "std::string deviceStatusJson("))
    model = _literals(_body(plan, "std::string modelStatusJson("))
    plan_json = _literals(
        _body(plan, "std::string EngineMemoryPlan::toStatusJson() const"),
        {"deviceStatusJson(device_)": device, "modelStatusJson(model_)": model, "breakdown_.toStatusJson()": breakdown},
    )
    audit_json = _literals(_body(audit, "std::string MemoryAuditResult::toStatusJson() const"))
    batch = _literals(_body(status, "void appendBatch("))
    body = re.sub(
        r"appendBatch\(out, [^)]*\);",
        'out << "' + batch.replace('"', '\\"') + '";',
        _body(status, "std::string runtimeStatusJson("),
    )
    paths = _paths(_literals(body, {"plan.toStatusJson()": plan_json, "memoryAudit.toStatusJson()": audit_json}))
    return paths | {f"warmup.{step}" for step in WARMUP_STEPS}


def flatten(value: object, prefix: tuple[str, ...] = ()) -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            path = (*prefix, key)
            paths.add(".".join(path))
            paths |= flatten(child, path)
    return paths


@pytest.fixture(scope="module")
def status(tmp_path_factory) -> dict:
    with FakeSplash(tmp_path_factory.mktemp("contract"), args=("--no-webui", "--max-cache-disk", "1G")) as fake:
        code, body = fake.json("GET", "/status")
    assert code == 200
    assert isinstance(body, dict)
    return body


@pytest.mark.skipif(not ENGINE.exists(), reason="no ./splash reference clone")
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
    ):
        assert path in paths, path
    assert len(paths) > 250


@pytest.mark.skipif(not ENGINE.exists(), reason="no ./splash reference clone")
def test_fake_status_has_every_native_key_path(status):
    missing = native_status_paths() - flatten(status)
    assert not missing, sorted(missing)


def test_fake_status_has_server_groups(status):
    for group, fields in SERVER_GROUPS.items():
        assert group in status, group
        assert fields <= set(status[group]), (group, fields - set(status[group]))
    assert status["schema_version"] == 6
    assert status["vision"] is True and status["input_modalities"] == ["text", "image", "pdf"]
    # server/latency.py stages are histograms keyed by stage name.
    assert {"upload", "http_request", "http_ttft", "output_interval"} <= set(status["latency"])


def test_prefill_warmup_name_matches_engine_constant():
    geometry = REAL_SPLASH / "runtime" / "metal" / "abi" / "ExecutionGeometry.h"
    if not geometry.exists():
        pytest.skip("no ./splash reference clone")
    budget = re.search(r"#define SPLASH_PREFILL_TOKEN_BUDGET (\d+)u", geometry.read_text())
    assert budget is not None and f"prefill_{budget.group(1)}" == WARMUP_STEPS[0]

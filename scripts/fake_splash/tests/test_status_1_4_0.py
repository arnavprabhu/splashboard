"""Splash 1.4.0's /status additions in the fake: the streamed stores, a
Flash-Next target's KV on the SSD cache, YaRN, the context's bounds and the
verified rows; and the request errors and bounded ends that come with them.

The engine is driven in-process, so a Flash-Next target needs no installed
model."""

from __future__ import annotations

import argparse
import sys

import pytest
from test_status_contract import ENGINE, flatten, native_status_paths

from harness import PKG

sys.path.insert(0, str(PKG))

from server import fake_engine, fake_server, serve_options
from server.errors import APIError
from server.fake_shapes import FakeJob, anthropic_stop

FLASH_NEXT = "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF:Q2_0"
DENSE = "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL"
MOE = "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"


def engine(model: str, *args: str, **config: object) -> fake_engine.FakeEngine:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model")
    parser.add_argument("--port", type=int, default=8000)
    serve_options.add_serve_arguments(parser)
    # The launcher's own option, which the shared table leaves out.
    parser.add_argument("--language-only", action="store_true")
    settings = fake_server.settings_from_args(parser.parse_args(["--model", model, *args]))
    cfg = fake_engine.FakeConfig(load_seconds=0, tokens_per_second=5000, prefill_tokens_per_second=1e6)
    cfg.update(config)
    fake = fake_engine.FakeEngine(settings, cfg)
    fake.plan = fake_engine.MemoryPlan(settings, cfg)
    fake.max_context = fake.plan.automatic_context
    fake.disk_open = settings.cache_disk_quota > 0
    fake.ready = True
    return fake


def job(prompt: int = 40, max_new_tokens: int = 64) -> FakeJob:
    return FakeJob("chatcmpl-1", list(range(prompt)), "hello", max_new_tokens, last_user="hello")


def run(fake: fake_engine.FakeEngine, request: FakeJob) -> list[tuple]:
    return list(fake.generate(request))


def test_an_in_memory_target_streams_nothing():
    status = engine(MOE).status({})
    assert status["streamed_table"] is None and status["streamed_experts"] is None
    assert "kv_spill" not in status
    budget = status["memory_plan"]["budget"]
    assert budget["kv_spill"] is None and budget["resident_kv_bytes"] == 0
    assert budget["context_bounds"] == {
        "window": 262144,
        "resident": budget["context_bounds"]["resident"],
        "spilled": 0,
        "disk": 0,
        "bound": "model",
    }
    assert status["identity"]["cache"]["rope_scaling"] is None
    assert status["identity"]["cache"]["dtype"] == "q8s8_f32_scale_per_token_head_k_token_major_v_dimension_major"
    assert status["identity"]["kv"]["value_layout"] == "dimension_major"
    assert status["disk"]["writes_deferred_ms"] == 0
    assert status["images"]["embedding_evictions"] == 0


@pytest.mark.parametrize(
    ("model", "args", "rows"),
    [(DENSE, (), "all"), (MOE, (), "auto"), (FLASH_NEXT, (), "auto"), (MOE, ("--verify-rows", "all"), "all")],
)
def test_verify_rows(model, args, rows):
    assert engine(model, *args).status({})["verify_rows"] == rows


def test_yarn_past_the_native_window():
    status = engine(MOE, "--max-context", "1M").status({})
    rope = status["identity"]["cache"]["rope_scaling"]
    assert rope == {"type": "yarn", "factor": 1_000_000 / 262144, "original_context_tokens": 262144}
    assert status["memory_plan"]["budget"]["context_bounds"]["window"] == 1_000_000


def test_flash_next_reports_every_conditional_group():
    fake = engine(FLASH_NEXT, kv_spill_tokens=10)
    run(fake, job())
    status = fake.status({})
    assert status["identity"]["kv"]["value_layout"] == "token_major"
    assert status["identity"]["cache"]["dtype"].endswith("_v_token_major")
    assert status["streamed_table"]["table"] == "per_layer_token_embd.weight"
    assert status["streamed_table"]["decode"]["hit_rate"] > 0
    assert status["streamed_experts"]["decode"]["routed"] > 0 and status["streamed_experts"]["failure"] == ""
    assert status["kv_spill"]["requests"]["spill"] == 1
    assert status["kv_spill"]["pages"]["spilled_bytes"] > 0
    bounds = status["memory_plan"]["budget"]["context_bounds"]
    assert bounds["spilled"] == bounds["window"] and bounds["disk"] > 0
    if ENGINE.exists():
        conditional = {p for p in native_status_paths() if p.startswith(("streamed_", "kv_spill", "memory_plan"))}
        missing = conditional - flatten(status)
        assert not missing, sorted(missing)


def test_the_disk_bounds_a_flash_next_context():
    # 2 GiB past the floor holds about 160K tokens of spilled KV: more than
    # 32 GB of memory holds, less than YaRN's window.
    gib = fake_engine.GIB
    status = engine(FLASH_NEXT, "--max-context", "1M", volume_available=12 * gib, physical_memory=32 * gib).status({})
    bounds = status["memory_plan"]["budget"]["context_bounds"]
    assert bounds["resident"] < bounds["disk"] == 160_667 < bounds["window"] and bounds["bound"] == "disk"
    # A small explicit quota bounds it too, however much the volume has.
    quota = engine(FLASH_NEXT, "--max-context", "1M", "--max-cache-disk", "2G", physical_memory=32 * gib).status({})
    assert quota["memory_plan"]["budget"]["context_bounds"]["disk"] == 2 * gib // fake_engine.SPILL_BYTES_PER_TOKEN
    # Without the SSD cache its context is what memory holds.
    off = engine(FLASH_NEXT, "--max-cache-disk", "0").status({})
    assert "kv_spill" not in off and off["memory_plan"]["budget"]["context_bounds"]["spilled"] == 0


def test_ple_and_expert_memory_off_load_them_whole():
    status = engine(FLASH_NEXT, "--ple-memory", "off", "--expert-memory", "off").status({})
    assert status["streamed_table"] is None and status["streamed_experts"] is None
    # A 35B-A3B streams its experts only at a size given.
    status = engine(MOE, "--expert-memory", "8G").status({})
    assert status["streamed_experts"]["arena_bytes"] <= 8 * fake_engine.GIB


def test_kv_mode_marks_spilling_requests():
    fake = engine(FLASH_NEXT, kv_spill_tokens=100)
    small, large = job(prompt=10, max_new_tokens=20), job(prompt=500)
    fake._admit(small)
    fake._admit(large)
    modes = [r["kv_mode"] for r in fake.status({})["active_requests"]]
    assert modes == ["resident", "spill"]
    # An in-memory target's rows carry no kv_mode.
    other = engine(MOE)
    other._admit(small)
    assert "kv_mode" not in other.status({})["active_requests"][0]


@pytest.mark.parametrize(
    ("mode", "status", "retry_after", "text"),
    [
        ("kv_write_budget", 503, 3600, "whose daily write budget (--max-cache-disk-writes) is spent"),
        ("kv_disk_quota", 400, 1, "of the SSD cache, which may hold active_limit_bytes="),
        ("kv_unreadable", 503, 1, "could not be read from the SSD cache"),
    ],
)
def test_ssd_cache_refusals(mode, status, retry_after, text):
    fake = engine(FLASH_NEXT)
    fake.set_mode(mode)
    with pytest.raises(APIError) as caught:
        run(fake, job())
    error = caught.value
    assert (error.status, error.code, error.retry_after) == (status, mode, retry_after)
    assert text in error.message
    refused = fake.status({})["kv_spill"]["requests"]["refused"]
    assert refused["write_budget"] == (mode == "kv_write_budget")
    assert refused["quota"] == (mode == "kv_disk_quota")


def test_a_bounded_end():
    fake = engine(FLASH_NEXT)
    fake.set_mode("bounded")
    events = run(fake, job(max_new_tokens=400))
    result = events[-1][1]
    assert result.reason == "length" and result.bounded and result.completion_tokens < 400
    assert anthropic_stop(result, [], False) == "model_context_window_exceeded"
    assert fake.status({})["kv_spill"]["requests"]["bounded"] == 1
    fake.set_mode("normal")
    result = run(fake, job(max_new_tokens=2))[-1][1]
    assert result.reason == "length" and not result.bounded
    assert anthropic_stop(result, [], False) == "max_tokens"


def test_an_expert_load_failure_is_reported():
    status = engine(FLASH_NEXT, expert_failure="cannot read expert 3 of layer 7").status({})
    assert status["streamed_experts"]["failure"] == "cannot read expert 3 of layer 7"

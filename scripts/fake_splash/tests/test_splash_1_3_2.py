"""Behaviour Splash 1.3.2 changed that the GUI reads: the automatic SSD cache and
its startup lines, the new serve flags, POST /v1/memory/release, the new /status
keys (disk limits, active requests, weights.restoring), the Started log line,
per-request speculation, thinking budgets and the looser request checks."""

from __future__ import annotations

import http.client
import json
import re
import threading
import time

import pytest
from test_startup_and_signals import TS, run_cli
from test_status_contract import ENGINE, native_status_paths

from conftest import chat_body, sse
from install import models, upstream

GIB = 1 << 30
MOE_STATE_MIB = 96


def _slow_stream(fake, body: dict) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    """Start a streaming request and return once its response has begun."""
    connection = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=30)
    connection.request(
        "POST", "/v1/chat/completions", json.dumps({**body, "stream": True}), {"Content-Type": "application/json"}
    )
    response = connection.getresponse()
    assert response.status == 200
    response.read1(64)
    return connection, response


def test_the_automatic_ssd_cache_is_on_by_default(make_engine):
    fake = make_engine()
    disk = fake.json("GET", "/status")[1]["disk"]
    assert disk["capacity_bytes"] == 32 * GIB and disk["limit_bytes"] == 32 * GIB
    assert disk["volume_available_bytes"] == 200 * GIB
    assert (disk["writes_gated"], disk["write_budget_bytes"]) == ("no", 0)
    plan = fake.json("GET", "/status")[1]["memory_plan"]
    assert plan["budget"]["state_staging_bytes"] == MOE_STATE_MIB << 20
    assert plan["model"]["state_format"] == "fp32"


def test_max_cache_disk_0_turns_the_cache_off(make_engine):
    fake = make_engine(args=["--no-webui", "--max-cache-disk", "0"])
    status = fake.json("GET", "/status")[1]
    assert (status["disk"]["capacity_bytes"], status["disk"]["limit_bytes"]) == (0, 0)
    assert status["memory_plan"]["budget"]["state_staging_bytes"] == 0
    assert not any("SSD cache" in line for line in fake.stderr)


def test_a_full_volume_turns_the_automatic_cache_off(make_engine):
    fake = make_engine(env={"FAKE_SPLASH_VOLUME_AVAILABLE": "12G"})
    fake.wait_for_line(
        TS + r"SSD cache off: 12\.0 GiB available on /System/Volumes/Data is too little beyond the 10\.0 GiB "
        r"it keeps free; the state staging the plan set aside stays unused\.$",
        stream="stderr",
    )
    assert fake.json("GET", "/status")[1]["disk"]["capacity_bytes"] == 0


def test_the_volume_limits_the_cache_and_a_write_budget_is_reported(make_engine):
    fake = make_engine(
        args=["--no-webui", "--max-cache-disk", "64G", "--max-cache-disk-writes", "100G"],
        env={"FAKE_SPLASH_VOLUME_AVAILABLE": "50G"},
    )
    line = fake.wait_for_line(r"SSD cache: up to ", stream="stderr")
    # From 1.4.0 a size is held as given while the volume keeps its floor: all its room.
    assert re.search(
        r"SSD cache: up to 40\.0 GiB of temporary files in \S+ \(the 40\.0 GiB its volume has beyond "
        r"the 10\.0 GiB it keeps free\); a write budget of 100\.0 GiB a day, which the states conversations resume "
        r"from and KV pages pass, starting with a quarter of a day's; KV pages of",
        line,
    )
    disk = fake.json("GET", "/status")[1]["disk"]
    assert (disk["capacity_bytes"], disk["limit_bytes"]) == (64 * GIB, 40 * GIB)
    assert disk["write_budget_bytes"] == 25 * GIB


def test_a_quota_too_small_for_a_state_sets_no_staging_aside(make_engine):
    fake = make_engine(args=["--no-webui", "--max-cache-disk", "10M"])
    fake.wait_for_line(
        TS + rf"SSD cache off: its quota holds no state of {MOE_STATE_MIB} MiB; no state staging is set aside\.$",
        stream="stderr",
    )
    assert fake.json("GET", "/status")[1]["memory_plan"]["budget"]["state_staging_bytes"] == 0


def test_bf16_states_halve_the_state(make_engine):
    fake = make_engine(args=["--no-webui", "--state-format", "bf16"])
    plan = fake.json("GET", "/status")[1]["memory_plan"]
    assert plan["model"]["state_format"] == "bf16"
    assert plan["budget"]["lane_state_bytes"] == (MOE_STATE_MIB // 2) << 20
    fake.wait_for_line(rf"states of {MOE_STATE_MIB // 2} MiB", stream="stderr")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("Time to answer.", "--thinking-end-text must end with </think> and hold no other special token"),
        ("word " * 70 + "</think>", "--thinking-end-text takes 71 tokens; the most is 64"),
    ],
)
def test_a_thinking_end_text_the_engine_refuses_ends_the_start(make_engine, text, message):
    fake = make_engine(args=["--no-webui", "--thinking-end-text", text], wait=False)
    assert fake.process.wait(20) == 1
    fake.wait_for_line(TS + re.escape(f"Error · {message}") + "$", stream="stderr")
    assert not any("Chat template" in line or "Stopping" in line for line in fake.stdout)


def test_thinking_token_budgets(make_engine, tmp_path):
    fake = make_engine(base=tmp_path / "default")
    capped = fake.json("POST", "/v1/chat/completions", chat_body(thinking_token_budget=2))[1]
    assert capped["usage"]["completion_tokens_details"]["reasoning_tokens"] == 2
    free = fake.json("POST", "/v1/chat/completions", chat_body(thinking_token_budget=-1))[1]
    assert free["usage"]["completion_tokens_details"]["reasoning_tokens"] > 2
    status, error = fake.json("POST", "/v1/completions", {"prompt": "x", "thinking_token_budget": 5})
    assert (status, error["error"]["message"]) == (400, "thinking_token_budget is not supported for text completions")
    server = make_engine(base=tmp_path / "server", args=["--no-webui", "--thinking-token-budget", "0"])
    none = server.json("POST", "/v1/chat/completions", chat_body())[1]
    assert none["usage"]["completion_tokens_details"]["reasoning_tokens"] == 0
    lifted = server.json("POST", "/v1/chat/completions", chat_body(thinking_token_budget=-1))[1]
    assert lifted["usage"]["completion_tokens_details"]["reasoning_tokens"] > 0


def test_memory_release(make_engine):
    fake = make_engine(env={"FAKE_SPLASH_RESTORE_SECONDS": "1.5", "FAKE_SPLASH_TOKS": "40"})
    code, body = fake.json("POST", "/v1/memory/release")
    assert code == 200
    assert body == {
        "released": True,
        "weights": {
            "idle_release_seconds": 600.0,
            "released": True,
            "restoring": False,
            "restores": 0,
            "restore_failures": 0,
        },
    }
    fake.wait_for_line(r"^Weights released on request; the next request restores them$", stream="stderr")
    # Already released: the answer is the same, and nothing more is logged.
    assert fake.json("POST", "/v1/memory/release", {})[0] == 200
    assert sum("released on request" in line for line in fake.stderr) == 1
    status, error = fake.json("POST", "/v1/memory/release", {"now": True})
    assert (status, error["error"]["message"]) == (400, "this endpoint takes no fields")
    # A request restores the weights; while it does, a release is refused.
    worker = threading.Thread(target=fake.json, args=("POST", "/v1/chat/completions", chat_body()))
    worker.start()
    deadline = time.monotonic() + 5
    while not fake.json("GET", "/status")[1]["weights"]["restoring"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert fake.json("GET", "/status")[1]["weights"]["released"] is True
    code, error = fake.json("POST", "/v1/memory/release")
    assert code == 409
    assert error["error"] == {
        "message": "the engine is serving requests or restoring its weights; it releases them only while idle",
        "type": "invalid_request_error",
        "code": "engine_busy",
    }
    worker.join(20)
    weights = fake.json("GET", "/status")[1]["weights"]
    assert (weights["released"], weights["restoring"], weights["restores"]) == (False, False, 1)
    # While a request is in service, too.
    connection, response = _slow_stream(fake, chat_body(max_tokens=200))
    assert fake.json("POST", "/v1/memory/release")[0] == 409
    response.close()
    connection.close()
    # A 409 is an answer about the engine's state: the console does not log it.
    assert not any("Error · engine_busy" in line for line in fake.stderr)


def test_memory_release_needs_the_api_key(make_engine):
    fake = make_engine(api_key="k")
    assert fake.request("POST", "/v1/memory/release", auth=False)[0] == 401
    assert fake.json("POST", "/v1/memory/release")[0] == 200


@pytest.mark.parametrize(
    ("mode", "status", "code"),
    [("engine_recovering", 503, "engine_recovering"), ("engine_failed", 500, "engine_failed")],
)
def test_memory_release_is_refused_like_a_request(make_engine, mode, status, code):
    # server/server.py: the release route is not prompt-only, so the backend's
    # refusal answers it before the handler runs.
    fake = make_engine()
    fake.set_mode(mode)
    answer, error = fake.json("POST", "/v1/memory/release")
    assert (answer, error["error"]["code"]) == (status, code)
    assert fake.json("GET", "/status")[1]["weights"]["released"] is False


def test_active_requests(make_engine):
    fake = make_engine(env={"FAKE_SPLASH_TOKS": "40"})
    assert fake.json("GET", "/status")[1]["active_requests"] == []
    connection, response = _slow_stream(fake, chat_body(max_tokens=300, priority="background"))
    deadline = time.monotonic() + 5
    while True:
        active = fake.json("GET", "/status")[1]["active_requests"]
        if active and active[0]["phase"] == "decode" and active[0]["generated_tokens"]:
            break
        assert time.monotonic() < deadline
        time.sleep(0.05)
    (request,) = active
    assert request["priority"] == "background" and request["max_new_tokens"] == 300
    assert request["prompt_processed"] == request["prompt_tokens"] > 0
    assert request["age_ms"] >= 0 and isinstance(request["id"], int)
    if ENGINE.exists():
        fields = {p.removeprefix("active_requests.") for p in native_status_paths() if p.startswith("active_requests.")}
        # kv_mode is written only where the target's KV can spill (1.4.0).
        assert set(request) == fields - {"kv_mode"}
    response.close()
    connection.close()
    deadline = time.monotonic() + 5
    while fake.json("GET", "/status")[1]["active_requests"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)


def test_a_score_only_request_is_active_too(make_engine):
    # A slow prefill keeps the judgment in service long enough to see it.
    fake = make_engine(env={"FAKE_SPLASH_PREFILL_TPS": "50"})
    body = {
        "id": "row-1",
        "state": "slow " * 100,
        "question": "Is it daytime?",
        "options": [{"id": "yes", "description": "yes"}, {"id": "no", "description": "no"}],
    }
    worker = threading.Thread(target=fake.json, args=("POST", "/v1/judgments", body))
    worker.start()
    deadline = time.monotonic() + 5
    while not (active := fake.json("GET", "/status")[1]["active_requests"]):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    (request,) = active
    assert request["max_new_tokens"] == 0 and request["generated_tokens"] == 0
    worker.join(20)
    assert fake.json("GET", "/status")[1]["active_requests"] == []


def test_started_line_and_speculation(make_engine):
    fake = make_engine()
    body = fake.json("POST", "/v1/chat/completions", chat_body(reasoning_effort="low", stream=False))[1]
    fake.wait_for_line(r"Started · input \d+ · cached \d+ · effort low · thinking on$", stream="stdout")
    done = fake.wait_for_line(r"Done · input \d", stream="stdout")
    assert re.search(r" · \d+\.\d tok/cycle$", done)
    timings = body["timings"]
    assert timings["draft_n"] > 0 and 0 < timings["draft_n_accepted"] < body["usage"]["completion_tokens"]
    fake.json("POST", "/v1/chat/completions", chat_body(chat_template_kwargs={"enable_thinking": False}))
    fake.wait_for_line(r"Started · .* · effort absent · enable_thinking false · thinking off$", stream="stdout")


def test_stop_sequences_combine_with_tools_and_structured_output(engine):
    tool = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    assert engine.json("POST", "/v1/chat/completions", chat_body(tools=[tool], stop=["\n\n"]))[0] == 200
    schema = {"type": "json_schema", "json_schema": {"name": "a", "schema": {"type": "object"}}}
    assert engine.json("POST", "/v1/chat/completions", chat_body(response_format=schema, stop="END"))[0] == 200


def test_null_fields_and_include_usage(engine):
    status, body = engine.json(
        "POST", "/v1/completions", {"prompt": "hello", "max_tokens": None, "model": None, "best_of": None}
    )
    assert status == 200 and body["usage"]["completion_tokens"] <= 16
    events = sse(engine, "/v1/chat/completions", chat_body(stream=True, stream_options={"include_usage": None}))
    assert not any(isinstance(data, dict) and data.get("usage") for _, data in events)


def test_cache_salt_keeps_prompts_apart(engine):
    prompt = chat_body("salted " * 120)
    engine.json("POST", "/v1/chat/completions", prompt)
    warm = engine.json("POST", "/v1/chat/completions", prompt)[1]
    assert warm["timings"]["cache_n"] > 0
    cold = engine.json("POST", "/v1/chat/completions", {**prompt, "cache_salt": "bench-1"})[1]
    assert cold["timings"]["cache_n"] == 0
    again = engine.json("POST", "/v1/chat/completions", {**prompt, "cache_salt": "bench-1"})[1]
    assert again["timings"]["cache_n"] > 0


def test_messages_pass_thinking_budget_and_template_kwargs(engine):
    body = {
        "model": "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M",
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": "hi"}],
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    status, message = engine.json("POST", "/v1/messages", body)
    assert status == 200 and [c["type"] for c in message["content"]] == ["text"]


def test_serve_help_matches_the_new_flags(tmp_path):
    help_text = run_cli(tmp_path, "serve", "--help", env={"COLUMNS": "200"}).stdout
    # 1.4.0's wording: a size is held as given while the volume keeps its floor.
    assert "is held as given while the volume keeps that floor; 0 disables (default: auto)" in help_text
    assert "(default: 0, no limit)" in help_text and "{fp32,bf16}" in help_text


class _GgufRepo:
    def __init__(self, names: list[str]):
        self.files = {name: name for name in names}

    def json(self, name):
        return {"text_config": {}}


def test_ud_q8_k_xl_loads_and_only_a_whole_bf16_model_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(upstream, "check_model", lambda *a, **k: "family")
    names = ["Qwen3.8-27B-UD-Q8_K_XL.gguf", "Qwen3.8-27B-BF16.gguf", "Qwen3.8-27B-Q4_0.gguf"]
    target = upstream._gguf_target(_GgufRepo(names), "UD-Q8_K_XL", True, tmp_path)
    assert target.format == "gguf" and "target/Qwen3.8-27B-UD-Q8_K_XL.gguf" in target.files
    with pytest.raises(models.ModelError, match=r"stores every projection and expert unquantized \(BF16, F16"):
        upstream._gguf_target(_GgufRepo(names), "BF16", True, tmp_path)


class _SafetensorsRepo:
    def __init__(self, config: dict):
        self.files = {"config.json", "model.safetensors", "tokenizer.json"}
        self.config = {"text_config": {}, **config}

    def json(self, name):
        return self.config


def _compressed(weights: dict) -> dict:
    """A compressed-tensors NVFP4 config with one group holding `weights`."""
    group = {"targets": ["Linear"], "weights": weights}
    return {
        "quantization_config": {
            "quant_method": "compressed-tensors",
            "format": "nvfp4-pack-quantized",
            "config_groups": {"group_0": group},
        }
    }


@pytest.mark.parametrize(
    "config",
    [
        {"quantization": {"bits": 4, "group_size": 16, "mode": "nvfp4"}},
        {"quantization_config": {"quant_method": "modelopt", "quant_algo": "NVFP4", "group_size": 16}},
        {"quantization_config": {"quant_method": "modelopt", "quant_algo": "FP8"}},
        {
            "quantization_config": {
                "quant_method": "modelopt",
                "quant_algo": "MIXED_PRECISION",
                "quantized_layers": {"model.layers.0.mlp.down_proj": {"quant_algo": "FP8"}},
            }
        },
        {
            "quantization_config": {
                "quant_method": "compressed-tensors",
                "format": "nvfp4-pack-quantized",
                "config_groups": {
                    "group_0": {
                        "targets": ["Linear"],
                        "weights": {"type": "float", "num_bits": 4, "strategy": "tensor_group", "group_size": 16},
                    }
                },
            }
        },
    ],
)
def test_nvfp4_and_fp8_checkpoints_load(config, monkeypatch, tmp_path):
    monkeypatch.setattr(upstream, "check_model", lambda *a, **k: "family")
    upstream._mlx_target(_SafetensorsRepo(config), False, tmp_path)


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({}, "or nvfp4), an NVFP4 or FP8 checkpoint of Model Optimizer or compressed-tensors, or a supported GGUF"),
        (
            {"quantization": {"bits": 4, "group_size": 32, "mode": "nvfp4"}},
            "quantization is nvfp4 4-bit in groups of 32",
        ),
        (
            {"quantization_config": {"quant_method": "modelopt", "quant_algo": "INT4_AWQ"}},
            "quantization_config is INT4_AWQ; Model Optimizer weights load as NVFP4, W4A16_NVFP4 or FP8",
        ),
        (
            {
                "quantization_config": {
                    "quant_method": "modelopt",
                    "quant_algo": "NVFP4",
                    "quantized_layers": {"model.layers.0.mlp.down_proj": {"quant_algo": "INT8_SQ"}},
                }
            },
            "quantization_config quantized_layers model.layers.0.mlp.down_proj is INT8_SQ;",
        ),
        (
            _compressed({"type": "float", "num_bits": 4, "strategy": "tensor_group", "group_size": 32}),
            "config_groups group_0 is nvfp4-pack-quantized: float 4-bit by tensor_group in groups of 32; "
            "compressed-tensors weights load as NVFP4",
        ),
        (
            _compressed(
                {"type": "float", "num_bits": 4, "strategy": "tensor_group", "group_size": 16, "symmetric": False}
            ),
            "config_groups group_0 is asymmetric or ordered by activation groups; Splash loads symmetric",
        ),
        (
            _compressed(
                {"type": "float", "num_bits": 4, "strategy": "tensor_group", "group_size": 16, "actorder": "group"}
            ),
            "config_groups group_0 is asymmetric or ordered by activation groups;",
        ),
        ({"quantization_config": {"quant_method": "gptq", "bits": 4}}, "this model requires an MLX checkpoint"),
    ],
)
def test_other_safetensors_quantizations_are_refused(config, message, monkeypatch, tmp_path):
    monkeypatch.setattr(upstream, "check_model", lambda *a, **k: "family")
    with pytest.raises(models.ModelError, match=re.escape(message)):
        upstream._mlx_target(_SafetensorsRepo(config), False, tmp_path)

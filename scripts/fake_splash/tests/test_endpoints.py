"""Every HTTP endpoint, non-streaming and streaming, against the real shapes."""

from __future__ import annotations

import threading
import time

from conftest import chat_body, sse

MODEL = "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"

# The `/status` fields the GUI reads, with Status.cpp's real nesting (identity.cache/kv).
APPENDIX_B = {
    "identity": {
        "cache": ["build_id", "loaded_model_layout_sha256"],
        "kv": ["target_model_sha256", "format", "quantization"],
    },
    "requests": ["submitted", "completed", "cancelled", "failed"],
    "metrics": [
        "ttft_ms",
        "itl_ms",
        "prefill_input_tokens",
        "prefill_wall_ms",
        "prefill_tokens_per_second",
        "decode_output_tokens",
        "decode_wall_ms",
        "decode_cycle_ms",
        "decode_tokens_per_second",
        "drafted_tokens",
        "accepted_draft_tokens",
        "draft_acceptance_rate",
        "capacity_failures",
        "metal_failures",
    ],
    "memory_actual": ["allocated_bytes", "current_bytes", "peak_bytes"],
    "memory_governor": [
        "limit_bytes",
        "charged_bytes",
        "headroom_bytes",
        "growth_allowed",
        "denied_reservations",
        "system_pressure",
        "host_available_bytes",
        "host_reserve_bytes",
        "host_headroom_bytes",
    ],
    "kv": [
        "pages_allocated",
        "pages_active",
        "pages_cache",
        "pages_free",
        "allocated_bytes",
        "reclaimable_bytes",
        "extent_allocations",
        "extent_releases",
    ],
    "state": [
        "entries",
        "in_use",
        "in_use_evictions",
        "bytes",
        "checkpoint_entries",
        "checkpoint_bytes",
        "disk_hits",
        "disk_promotions",
        "offloads",
        "offload_failures",
    ],
    "disk": [
        "capacity_bytes",
        "used_bytes",
        "file_bytes",
        "read_bytes",
        "written_bytes",
        "kv_blocks",
        "kv_bytes",
        "kv_demotions",
        "kv_demotion_failures",
        "kv_demotions_refused",
        "kv_restores",
        "kv_restore_failures",
        "kv_pending_pages",
        "persistent",
        "kv_copies",
        "kv_copy_failures",
        "taken_back",
        "write_behind",
    ],
    "cache": [
        "hits",
        "cold_misses",
        "hit_rate",
        "kv_hit_tokens",
        "kv_disk_hit_tokens",
        "reused_tokens",
        "lost_state_misses",
        "resource_suspensions",
        "priority_suspensions",
        "resource_resumptions",
        "replay_state_publication_failures",
    ],
    "admission": [
        "waiting",
        "waiting_memory",
        "waiting_concurrency",
        "held_behind_refusal",
        "restoring",
        "suspended",
        "draining",
        "oldest_wait_ms",
    ],
    "scheduler": [
        "queued",
        "waiting_resources",
        "waiting_prefix",
        "prefilling",
        "decoding",
        "waiting_mask",
        "decode_batches_by_width",
    ],
    "loop": ["max_tick_ms"],
    "images": ["encodes", "embedding_reuses", "arena_bytes", "cached_bytes"],
    "metal": ["healthy", "failure_reason"],
    "transport": [
        "ready",
        "recovering",
        "stopped",
        "pending",
        "pending_limit",
        "restarts",
        "last_crash_trace",
        "status_stale",
        "status_age_ms",
    ],
    "latency": [
        "http_ttft",
        "preparation",
        "preparation_queue",
        "template",
        "tokenization",
        "grammar",
        "images",
        "http_request",
        "upload",
        "native_queue",
        "output_interval",
    ],
    "chat_template": ["later_system"],
    "http": ["requests", "request_body_bytes", "max_request_bytes", "token_counts", "connections"],
    "instance": ["id", "pid", "model", "host", "port", "started_at"],
    "response_store": ["entries", "bytes", "budget_bytes", "evictions", "hits", "misses"],
    "tokenizer_cache": ["enabled", "entries", "bytes", "budget_bytes", "capacity", "hits", "reused_tokens"],
    "grammar_cache": ["entries", "capacity", "source_bytes", "source_budget_bytes", "hits", "misses"],
}


def test_health_and_ready(engine):
    assert engine.json("GET", "/health") == (200, {"status": "ok"})
    assert engine.json("GET", "/ready") == (200, {"status": "ready"})


def test_status_has_every_appendix_b_field(engine):
    status, body = engine.json("GET", "/status")
    assert status == 200
    assert body["schema_version"] == 6
    for key in ("ready", "maximum_context_tokens", "memory_pressure", "vision", "input_modalities"):
        assert key in body
    assert body["maximum_context_tokens"] == 262144
    assert body["input_modalities"] == ["text", "image", "pdf"]
    for group, wanted in APPENDIX_B.items():
        assert group in body, group
        if isinstance(wanted, dict):
            for sub, keys in wanted.items():
                assert set(keys) <= body[group][sub].keys(), (group, sub)
        else:
            assert set(wanted) <= body[group].keys(), (group, set(wanted) - body[group].keys())
    assert set(body["disk"]["taken_back"]) == {"states", "kv_blocks", "bytes", "left_behind"}
    assert set(body["disk"]["write_behind"]) == {"waiting", "durable", "unneeded", "refused"}
    assert set(body["scheduler"]["decode_batches_by_width"]) == {"b1", "b2", "b3", "b4"}
    assert body["identity"]["kv"]["quantization"] == "symmetric_int8"
    assert body["instance"]["port"] == engine.port
    assert body["transport"]["recovering"] is False and body["transport"]["stopped"] is False


def test_status_counters_advance_while_generating(engine):
    before = engine.json("GET", "/status")[1]
    seen_decoding = []

    def poll():
        for _ in range(60):
            seen_decoding.append(engine.json("GET", "/status")[1]["scheduler"]["decoding"])
            time.sleep(0.01)

    engine.set_mode(config={"tokens_per_second": 100.0})
    try:
        poller = threading.Thread(target=poll)
        poller.start()
        assert engine.json("POST", "/v1/chat/completions", chat_body("count me in please"))[0] == 200
        poller.join()
    finally:
        engine.set_mode(config={"tokens_per_second": 2000.0})
    after = engine.json("GET", "/status")[1]
    assert max(seen_decoding) >= 1
    assert after["requests"]["submitted"] == before["requests"]["submitted"] + 1
    assert after["requests"]["completed"] == before["requests"]["completed"] + 1
    assert after["metrics"]["decode_output_tokens"] > before["metrics"]["decode_output_tokens"]
    assert after["metrics"]["ttft_ms"]["samples"] > before["metrics"]["ttft_ms"]["samples"]


def test_metrics_prometheus(engine):
    status, headers, data = engine.request("GET", "/metrics")
    assert status == 200
    assert headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
    text = data.decode()
    assert 'splash_info{runtime="native"} 1' in text
    assert 'splash_memory_pressure{state="normal"} 1' in text
    assert "splash_requests_submitted_total" in text
    assert "# TYPE splash_http_ttft_seconds histogram" in text
    assert "splash_disk" not in text  # disk fields are /status only


def test_models_listing_and_lookup(engine):
    status, headers, _ = engine.request("GET", "/v1/models")
    assert status == 200 and headers["x-typesafe-request-id"].startswith("req_")
    body = engine.json("GET", "/v1/models")[1]
    entry = body["data"][0]
    assert entry == {
        "id": MODEL,
        "object": "model",
        "created": 0,
        "owned_by": "splash",
        "max_model_len": 262144,
        "context_length": 262144,
        "vision": True,
        "input_modalities": ["text", "image", "pdf"],
    }
    assert body["models"] == [{"name": MODEL, "description": "Splash resident model", "release_date": ""}]
    assert engine.json("GET", f"/v1/models/{MODEL}")[1]["id"] == MODEL
    status, error = engine.json("GET", "/v1/models/nope/nope")
    assert status == 404 and error["error"]["code"] == "model_not_found"


def test_chat_non_stream(engine):
    status, body = engine.json("POST", "/v1/chat/completions", chat_body("tell me about tides", max_tokens=100))
    assert status == 200
    assert body["object"] == "chat.completion" and body["id"].startswith("chatcmpl-")
    message = body["choices"][0]["message"]
    assert message["role"] == "assistant"
    assert "tell me about tides" in message["content"]
    assert message["reasoning_content"]
    assert body["choices"][0]["finish_reason"] == "stop"
    usage = body["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert usage["completion_tokens_details"]["reasoning_tokens"] > 0
    assert set(body["timings"]) == {
        "prompt_n",
        "prompt_ms",
        "prompt_per_second",
        "predicted_n",
        "predicted_ms",
        "predicted_per_second",
        "cache_n",
    }
    assert body["metrics"]["cache"]["status"] in ("hit", "miss")


def test_chat_stream_shape(engine):
    events = sse(
        engine,
        "/v1/chat/completions",
        chat_body(
            "stream please",
            stream=True,
            return_progress=True,
            stream_options={"include_usage": True},
            reasoning_effort="none",
        ),
    )
    data = [d for _, d in events if not isinstance(d, str)]
    assert data[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert any("prompt_progress" in d for d in data)
    progress = next(d["prompt_progress"] for d in data if "prompt_progress" in d)
    assert set(progress) == {"total", "cache", "processed", "time_ms"}
    text = "".join(d["choices"][0]["delta"].get("content", "") for d in data if d["choices"])
    assert "stream please" in text
    final = [d for d in data if d["choices"] and d["choices"][0]["finish_reason"]]
    assert len(final) == 1 and "timings" in final[0] and final[0]["choices"][0]["delta"] == {}
    usage_chunk = data[-1]
    assert usage_chunk["choices"] == [] and "usage" in usage_chunk and "metrics" in usage_chunk
    assert events[-1] == (None, "[DONE]")


def test_chat_stream_without_include_usage_still_has_timings(engine):
    events = sse(engine, "/v1/chat/completions", chat_body("x", stream=True))
    data = [d for _, d in events if not isinstance(d, str)]
    assert "timings" in data[-1] and not any("usage" in d for d in data)


def test_chat_tool_call(engine):
    tool = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        },
    }
    body = engine.json(
        "POST", "/v1/chat/completions", chat_body("weather in Paris", tools=[tool], tool_choice="required")
    )[1]
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["type"] == "function" and call["function"]["name"] == "get_weather"
    assert call["id"].startswith("call_")
    events = sse(
        engine, "/v1/chat/completions", chat_body("weather", tools=[tool], tool_choice="required", stream=True)
    )
    deltas = [d["choices"][0]["delta"] for _, d in events if not isinstance(d, str) and d["choices"]]
    calls = [d["tool_calls"][0] for d in deltas if "tool_calls" in d]
    assert calls[0]["function"] == {"name": "get_weather"} and calls[0]["id"].startswith("call_")
    assert "".join(c["function"].get("arguments", "") for c in calls[1:]).startswith("{")


def test_chat_length_and_stop(engine):
    body = engine.json(
        "POST", "/v1/chat/completions", chat_body("one two three", max_tokens=3, reasoning_effort="none")
    )[1]
    assert body["choices"][0]["finish_reason"] == "length"
    assert body["usage"]["completion_tokens"] == 3
    body = engine.json("POST", "/v1/chat/completions", chat_body("alpha beta", stop=["fake"], reasoning_effort="none"))[
        1
    ]
    assert body["choices"][0]["message"]["content"] == "This is a "
    assert body["choices"][0]["finish_reason"] == "stop"


def test_chat_validation_errors(engine):
    cases = [
        (chat_body(temperature=3), "temperature must be a number in [0, 2]"),
        (chat_body(n=2), "n and logprobs are not currently supported"),
        (chat_body(logit_bias={"1": 2}), "logit_bias is not supported"),
        (chat_body(return_progress=True), "return_progress requires stream: true and must be a boolean"),
        ({"messages": []}, "messages must be a non-empty array"),
        (chat_body(reasoning_effort="huge"), "invalid reasoning_effort"),
        (chat_body(priority="urgent"), "priority must be foreground, normal, or background"),
    ]
    for body, message in cases:
        status, error = engine.json("POST", "/v1/chat/completions", body)
        assert status == 400, body
        assert error == {
            "error": {"message": message, "type": "invalid_request_error", "code": "invalid_request_error"}
        }
    status, error = engine.json("POST", "/v1/chat/completions", chat_body(model="other/model"))
    assert status == 404 and error["error"]["code"] == "model_not_found"
    status, error = engine.json("POST", "/v1/chat/completions", chat_body(max_tokens=10**7))
    assert status == 400 and error["error"]["code"] == "context_length_exceeded"
    status, _, data = engine.request("POST", "/v1/chat/completions", headers={"Content-Type": "text/plain"}, body="x")
    assert status == 415 and b"Content-Type must be application/json" in data


def test_prefix_cache_hits_on_second_turn(engine):
    first = chat_body(" ".join(f"word{i}" for i in range(200)), reasoning_effort="none")
    body = engine.json("POST", "/v1/chat/completions", first)[1]
    follow = {
        "messages": [
            *first["messages"],
            {"role": "assistant", "content": body["choices"][0]["message"]["content"]},
            {"role": "user", "content": "and then?"},
        ],
        "reasoning_effort": "none",
    }
    second = engine.json("POST", "/v1/chat/completions", follow)[1]
    assert second["usage"]["prompt_tokens_details"]["cached_tokens"] >= 192
    assert second["timings"]["cache_n"] == second["usage"]["prompt_tokens_details"]["cached_tokens"]


def test_completions(engine):
    body = engine.json("POST", "/v1/completions", {"prompt": "Once upon a time"})[1]
    assert body["object"] == "text_completion" and body["id"].startswith("cmpl-")
    choice = body["choices"][0]
    assert choice["logprobs"] is None and choice["finish_reason"] in ("stop", "length")
    assert body["usage"]["completion_tokens"] <= 16  # default max_tokens
    events = sse(engine, "/v1/completions", {"prompt": [1, 2, 3], "stream": True, "max_tokens": 4})
    data = [d for _, d in events if not isinstance(d, str)]
    assert all(d["object"] == "text_completion" for d in data)
    assert data[-1]["choices"][0]["finish_reason"] == "length" and "timings" in data[-1]
    for field in ("echo", "suffix"):
        status, error = engine.json("POST", "/v1/completions", {"prompt": "x", field: True if field == "echo" else "s"})
        assert status == 400 and error["error"]["type"] == "invalid_request_error"


def test_responses_non_stream_store_and_chain(engine):
    status, first = engine.json("POST", "/v1/responses", {"input": "first question", "instructions": "be brief"})
    assert status == 200
    assert first["object"] == "response" and first["status"] == "completed" and first["store"] is True
    kinds = [item["type"] for item in first["output"]]
    assert kinds == ["reasoning", "message"]
    assert first["usage"]["input_tokens_details"].keys() == {"cached_tokens", "cache_write_tokens"}
    assert engine.json("GET", f"/v1/responses/{first['id']}") == (200, first)
    status, second = engine.json("POST", "/v1/responses", {"input": "follow up", "previous_response_id": first["id"]})
    assert status == 200 and second["previous_response_id"] == first["id"]
    assert second["usage"]["input_tokens"] > first["usage"]["input_tokens"]
    assert engine.json("DELETE", f"/v1/responses/{first['id']}") == (
        200,
        {"id": first["id"], "object": "response", "deleted": True},
    )
    status, error = engine.json("GET", f"/v1/responses/{first['id']}")
    assert status == 404 and error["error"]["code"] == "not_found_error"
    status, error = engine.json("POST", "/v1/responses", {"input": "x", "previous_response_id": first["id"]})
    assert status == 404 and error["error"]["code"] == "previous_response_not_found"
    unstored = engine.json("POST", "/v1/responses", {"input": "x", "store": False})[1]
    assert unstored["store"] is False
    assert engine.json("GET", f"/v1/responses/{unstored['id']}")[0] == 404


def test_responses_stream_events(engine):
    events = sse(engine, "/v1/responses", {"input": "streamed", "stream": True})
    names = [e for e, _ in events]
    assert names[:2] == ["response.created", "response.in_progress"]
    assert names[-1] == "response.completed"
    for name in (
        "response.output_item.added",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
    ):
        assert name in names
    sequence = [d["sequence_number"] for _, d in events]
    assert sequence == list(range(len(sequence)))
    final = events[-1][1]["response"]
    assert final["usage"]["output_tokens"] > 0
    assert engine.json("GET", f"/v1/responses/{final['id']}")[0] == 200


def test_messages_non_stream_and_stream(engine):
    body = {
        "model": MODEL,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": "hi claude"}],
        "thinking": {"type": "enabled", "budget_tokens": 100},
    }
    status, message = engine.json("POST", "/v1/messages", body)
    assert status == 200
    assert message["type"] == "message" and message["id"].startswith("msg_")
    assert [c["type"] for c in message["content"]] == ["thinking", "text"]
    assert message["stop_reason"] == "end_turn"
    assert set(message["usage"]) == {"input_tokens", "cache_read_input_tokens", "output_tokens"}
    events = sse(engine, "/v1/messages", {**body, "stream": True})
    names = [e for e, _ in events]
    assert names[0] == "message_start" and names[-1] == "message_stop"
    assert names[-2] == "message_delta"
    assert "content_block_start" in names and "content_block_stop" in names
    deltas = [d["delta"]["type"] for e, d in events if e == "content_block_delta"]
    assert "thinking_delta" in deltas and "text_delta" in deltas
    delta = events[-2][1]
    assert delta["delta"]["stop_reason"] == "end_turn" and delta["usage"]["output_tokens"] > 0
    assert all(d["type"] == e for e, d in events)


def test_messages_errors_use_anthropic_shape(engine):
    status, error = engine.json(
        "POST",
        "/v1/messages",
        {
            "model": MODEL,
            "max_tokens": 5,
            "messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "prefill"}],
        },
    )
    assert status == 400
    assert error == {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "a final assistant message (prefill) is not supported; end messages with a user turn",
        },
    }
    status, error = engine.json(
        "POST", "/v1/messages", {"model": MODEL, "messages": [{"role": "user", "content": "x"}]}
    )
    assert status == 400 and error["error"]["message"] == "max_tokens must be a positive integer"


def test_count_tokens_tokenize_apply_template(engine):
    status, counted = engine.json(
        "POST",
        "/v1/messages/count_tokens",
        {"model": MODEL, "messages": [{"role": "user", "content": "count these words"}]},
    )
    assert status == 200 and counted["input_tokens"] > 3
    status, tokens = engine.json("POST", "/tokenize", {"content": "a b c"})
    assert status == 200 and len(tokens["tokens"]) == 3
    assert (
        engine.json("POST", "/tokenize", {"content": "x", "with_pieces": True})[1]["error"]["message"]
        == "only with_pieces=false is supported"
    )
    status, rendered = engine.json("POST", "/apply-template", chat_body("templated"))
    assert status == 200
    assert rendered["prompt"].startswith("<|im_start|>user\ntemplated<|im_end|>")
    assert rendered["prompt"].endswith("<|im_start|>assistant\n<think>\n")


def test_judgments_and_systemone(engine):
    status, judged = engine.json(
        "POST",
        "/v1/judgments",
        {
            "id": "row-1",
            "state": "the sky is blue",
            "question": "Is it daytime?",
            "options": [{"id": "yes", "description": "yes"}, {"id": "no", "description": "no"}],
        },
    )
    assert status == 200
    assert judged["option_ids"] == ["yes", "no"] and abs(sum(judged["probabilities"]) - 1) < 1e-9
    assert judged["prompt_version"] == "direct-options-v1" and judged["usage"]["completion_tokens"] == 0
    status, headers, _ = engine.request("POST", "/v1/systemone", {"model": MODEL, "state": "s", "questions": {}})
    assert status == 422 and headers["x-typesafe-request-id"].startswith("req_")
    status, answered = engine.json(
        "POST",
        "/v1/systemone",
        {
            "model": MODEL,
            "state": "text",
            "questions": {
                "q1": {"type": "noul"},
                "q2": {"type": "choice", "criteria": {"a": "A", "b": "B"}},
                "q3": {"type": "score", "criteria": ["low", "high"]},
            },
        },
    )
    assert status == 200
    assert answered["answers"]["q1"]["type"] == "noul"
    assert answered["answers"]["q2"]["choice"] in ("a", "b")
    assert answered["answers"]["q3"]["legend"] == {"0": "low", "1": "high"}
    status, invalid = engine.json(
        "POST", "/v1/systemone", {"model": "x/y", "state": "s", "questions": {"q": {"type": "bad"}}}
    )
    assert status == 422 and invalid["detail"][0]["loc"] == ["body", "model"]


def test_last_requests_record_bodies(engine):
    engine.clear_requests()
    engine.json("POST", "/v1/chat/completions", chat_body("recorded", temperature=0.3, priority="background"))
    recorded = engine.last_requests()
    assert recorded[-1]["path"] == "/v1/chat/completions"
    assert recorded[-1]["body"]["temperature"] == 0.3 and recorded[-1]["body"]["priority"] == "background"
    assert recorded[-1]["status"] == 200
    assert engine.last_requests(n=1) == recorded[-1:]


def test_unknown_route_and_webui_disabled(engine):
    assert engine.json("GET", "/nope")[1] == {
        "error": {"message": "not found", "type": "invalid_request_error", "code": "not_found"}
    }
    assert engine.json("GET", "/")[0] == 404  # started with --no-webui

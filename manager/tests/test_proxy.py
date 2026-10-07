"""The public proxy end to end through the fake engine (SPEC §7).

Each test drives the manager's public routes and then reads what the engine
actually received from the fake's `/_fake/last_requests`, so injection, header
rewriting and model rewriting are checked on the wire, not on the helper.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from splash_gui.proxy.shapes import constrained, inject

from .fakeengine import MODEL, MODEL_27B, EngineHarness

H = Callable[..., EngineHarness]
CHAT = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}


def engine_bodies(h: EngineHarness, path: str) -> list[dict[str, Any]]:
    """Bodies the engine received on `path`, oldest first (status polls filtered out)."""
    return [
        r["body"]
        for r in h.last_engine_requests()
        if r["method"] == "POST" and r["path"].split("?")[0] == path and r["body"] is not None
    ]


def last_body(h: EngineHarness, path: str) -> dict[str, Any]:
    bodies = engine_bodies(h, path)
    assert bodies, f"the engine received nothing on {path}"
    return bodies[-1]


def last_engine_headers(h: EngineHarness, path: str) -> dict[str, str]:
    rows = [r for r in h.last_engine_requests() if r["path"].split("?")[0] == path]
    assert rows
    return {k.lower(): v for k, v in rows[-1]["headers"].items()}


def usage_row(h: EngineHarness, request_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rows = h.client.get("/api/admin/usage/requests", params={"request_id": request_id}).json()[
            "rows"
        ]
        if rows:
            return dict(rows[0])
        time.sleep(0.02)
    raise AssertionError(f"no usage row for {request_id}")


def model_settings(h: EngineHarness, model: str, **entry: Any) -> None:
    h.patch_settings({"models": {model: entry}})


# --- The §21 acceptance case -----------------------------------------------------------


def test_no_think_profile_from_an_external_client_is_injected_and_logged(h_ready: EngineHarness):
    h = h_ready
    response = h.client.post(
        "/v1/chat/completions",
        json={"model": f"{MODEL}:no-think", **CHAT},
        headers={"User-Agent": "curl/8.7.1"},
    )
    assert response.status_code == 200, response.text
    sent = last_body(h, "/v1/chat/completions")
    assert sent["reasoning_effort"] == "none"
    assert sent["model"] == MODEL, "the profile ID is rewritten to the real model ID"
    row = usage_row(h, response.headers["x-splash-request-id"])
    assert row["profile"] == "no-think"
    assert row["model"] == MODEL
    assert row["client"] == "curl"
    assert row["injected"] == {"reasoning_effort": "none"}
    assert row["status"] == 200
    assert row["prompt_tokens"] and row["completion_tokens"]


def test_an_explicit_field_is_never_overridden(h_ready: EngineHarness):
    h = h_ready
    response = h.client.post(
        "/v1/chat/completions",
        json={"model": f"{MODEL}:no-think", "reasoning_effort": "high", **CHAT},
    )
    assert response.status_code == 200, response.text
    assert last_body(h, "/v1/chat/completions")["reasoning_effort"] == "high"
    assert usage_row(h, response.headers["x-splash-request-id"])["injected"] is None


def test_a_null_field_is_filled(h_ready: EngineHarness):
    h = h_ready
    response = h.client.post(
        "/v1/chat/completions",
        json={"model": f"{MODEL}:deterministic", "temperature": None, **CHAT},
    )
    assert response.status_code == 200, response.text
    sent = last_body(h, "/v1/chat/completions")
    assert sent["temperature"] == 0 and sent["seed"] == 0
    assert usage_row(h, response.headers["x-splash-request-id"])["injected"] == {
        "temperature": 0,
        "seed": 0,
    }


def test_the_plain_model_id_gets_only_the_model_defaults(h_ready: EngineHarness):
    h = h_ready
    model_settings(h, MODEL, sampling_defaults={"temperature": 0.25})
    response = h.client.post("/v1/chat/completions", json={"model": MODEL, **CHAT})
    assert response.status_code == 200
    sent = last_body(h, "/v1/chat/completions")
    assert sent["temperature"] == 0.25 and "reasoning_effort" not in sent
    row = usage_row(h, response.headers["x-splash-request-id"])
    assert row["profile"] is None and row["injected"] == {"temperature": 0.25}


def test_a_profile_replaces_the_defaults_but_explicit_fields_win(h_ready: EngineHarness):
    h = h_ready
    model_settings(
        h,
        MODEL,
        sampling_defaults={"temperature": 0.25, "top_p": 0.5},
        profiles={"careful": {"temperature": 0.1}},
    )
    response = h.client.post(
        "/v1/chat/completions", json={"model": f"{MODEL}:careful", "top_p": 0.9, **CHAT}
    )
    assert response.status_code == 200, response.text
    sent = last_body(h, "/v1/chat/completions")
    assert sent["temperature"] == 0.1  # the profile replaces the default
    assert sent["top_p"] == 0.9  # the request's own value wins over the default


# --- Fields per API shape (SPEC §7.5 table) ------------------------------------------

OVERLAY = {
    "temperature": 0.3,
    "top_k": 7,
    "min_p": 0.05,
    "presence_penalty": 0.5,
    "seed": 11,
    "stop": ["STOP"],
    "priority": "background",
    "max_tokens": 9,
    "reasoning_effort": "low",
    "chat_template_kwargs": {"enable_thinking": False},
    "ignore_eos": False,
}


@pytest.fixture
def h_shapes(h_ready: EngineHarness) -> EngineHarness:
    model_settings(h_ready, MODEL, profiles={"all": OVERLAY})
    return h_ready


def test_chat_receives_every_chat_field(h_shapes: EngineHarness):
    h = h_shapes
    body = {"model": f"{MODEL}:all", "messages": [{"role": "user", "content": "hi"}]}
    assert h.client.post("/v1/chat/completions", json=body).status_code == 200
    sent = last_body(h, "/v1/chat/completions")
    for name in ("temperature", "top_k", "min_p", "presence_penalty", "seed", "stop", "priority"):
        assert sent[name] == OVERLAY[name], name
    assert sent["max_completion_tokens"] == 9 and "max_tokens" not in sent
    assert sent["reasoning_effort"] == "low"
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["ignore_eos"] is False


def test_chat_max_tokens_spelling_counts_as_explicit(h_shapes: EngineHarness):
    h = h_shapes
    body = {
        "model": f"{MODEL}:all",
        "max_tokens": 3,
        "messages": [{"role": "user", "content": "x"}],
    }
    assert h.client.post("/v1/chat/completions", json=body).status_code == 200
    sent = last_body(h, "/v1/chat/completions")
    assert sent["max_tokens"] == 3 and "max_completion_tokens" not in sent


def test_completions_get_max_tokens_but_no_chat_only_fields(h_shapes: EngineHarness):
    h = h_shapes
    response = h.client.post("/v1/completions", json={"model": f"{MODEL}:all", "prompt": "Once"})
    assert response.status_code == 200, response.text
    sent = last_body(h, "/v1/completions")
    assert sent["max_tokens"] == 9 and sent["min_p"] == 0.05 and sent["stop"] == ["STOP"]
    for absent in ("reasoning_effort", "chat_template_kwargs", "max_completion_tokens"):
        assert absent not in sent, absent


def test_responses_get_max_output_tokens_and_reasoning_effort(h_shapes: EngineHarness):
    h = h_shapes
    response = h.client.post("/v1/responses", json={"model": f"{MODEL}:all", "input": "hello"})
    assert response.status_code == 200, response.text
    sent = last_body(h, "/v1/responses")
    assert sent["max_output_tokens"] == 9
    assert sent["reasoning"] == {"effort": "low"}
    assert sent["min_p"] == 0.05
    for absent in ("reasoning_effort", "chat_template_kwargs", "ignore_eos", "max_tokens"):
        assert absent not in sent, absent
    row = usage_row(h, response.headers["x-splash-request-id"])
    assert row["injected"]["reasoning.effort"] == "low"


def test_responses_keep_other_reasoning_keys(h_shapes: EngineHarness):
    h = h_shapes
    body = {"model": f"{MODEL}:all", "input": "hi", "reasoning": {"summary": "auto"}}
    assert h.client.post("/v1/responses", json=body).status_code == 200
    assert last_body(h, "/v1/responses")["reasoning"] == {"summary": "auto", "effort": "low"}


def test_messages_get_only_what_anthropic_accepts(h_shapes: EngineHarness):
    h = h_shapes
    body = {
        "model": f"{MODEL}:all",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    }
    response = h.client.post("/v1/messages", json=body)
    assert response.status_code == 200, response.text
    sent = last_body(h, "/v1/messages")
    assert sent["temperature"] == 0.3 and sent["top_k"] == 7 and sent["seed"] == 11
    assert sent["priority"] == "background" and sent["max_tokens"] == 16
    for absent in (
        "min_p",
        "presence_penalty",
        "stop",
        "reasoning_effort",
        "chat_template_kwargs",
        "ignore_eos",
        "max_completion_tokens",
    ):
        assert absent not in sent, absent


def test_messages_thinking_only_when_the_profile_sets_it(h_ready: EngineHarness):
    h = h_ready
    thinking = {"type": "enabled", "budget_tokens": 1024}
    model_settings(h, MODEL, profiles={"think": {"thinking": thinking}})
    body = {"max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
    h.client.post("/v1/messages", json={"model": f"{MODEL}:think", **body})
    assert last_body(h, "/v1/messages")["thinking"] == thinking
    h.client.post("/v1/messages", json={"model": f"{MODEL}:no-think", **body})
    assert "thinking" not in last_body(h, "/v1/messages")


def test_count_tokens_tokenize_and_judgments_are_not_injected(h_shapes: EngineHarness):
    h = h_shapes
    count = h.client.post(
        "/v1/messages/count_tokens",
        json={"model": f"{MODEL}:all", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert count.status_code == 200, count.text
    sent = last_body(h, "/v1/messages/count_tokens")
    assert "temperature" not in sent and sent["model"] == MODEL
    tok = h.client.post("/tokenize", json={"model": f"{MODEL}:all", "content": "hello"})
    assert tok.status_code == 200, tok.text
    assert "temperature" not in last_body(h, "/tokenize")


def test_stop_and_ignore_eos_are_not_added_next_to_tools(h_shapes: EngineHarness):
    h = h_shapes
    tool = {"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}
    body = {
        "model": f"{MODEL}:all",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [tool],
    }
    response = h.client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200, response.text
    sent = last_body(h, "/v1/chat/completions")
    assert "stop" not in sent and "ignore_eos" not in sent
    assert sent["temperature"] == 0.3


@pytest.mark.parametrize(
    ("shape", "body", "expected"),
    [
        ("chat", {}, False),
        ("chat", {"tools": []}, False),
        ("chat", {"tools": [{"type": "function"}]}, True),
        ("chat", {"response_format": {"type": "text"}}, False),
        ("chat", {"response_format": {"type": "json_object"}}, True),
        ("responses", {"text": {"format": {"type": "json_schema"}}}, True),
        ("responses", {"text": {"format": {"type": "text"}}}, False),
    ],
)
def test_constrained(shape: Any, body: dict[str, Any], expected: bool) -> None:
    assert constrained(shape, body) is expected


def test_inject_unit_cases() -> None:
    body, injected = inject("chat", {"temperature": 1.0}, {"temperature": 0.1, "top_p": 0.5})
    assert body == {"temperature": 1.0, "top_p": 0.5} and injected == {"top_p": 0.5}
    assert inject("other", {}, {"temperature": 0.1}) == ({}, {})
    assert inject("chat", {"x": 1}, {}) == ({"x": 1}, {})
    body, injected = inject("responses", {"reasoning": None}, {"reasoning_effort": "max"})
    assert body["reasoning"] == {"effort": "max"} and injected == {"reasoning.effort": "max"}
    body, injected = inject(
        "responses", {"reasoning": {"effort": "low"}}, {"reasoning_effort": "max"}
    )
    assert body["reasoning"] == {"effort": "low"} and injected == {}
    body, injected = inject("messages", {}, {"max_tokens": 5, "reasoning_effort": "low"})
    assert body == {} and injected == {}


# --- Forwarding rules (SPEC §7.2) ----------------------------------------------------


def test_credentials_origin_and_host_are_rewritten(h_ready: EngineHarness):
    h = h_ready
    response = h.client.post(
        "/v1/chat/completions",
        json={"model": MODEL, **CHAT},
        headers={
            "Authorization": "Bearer user-secret",
            "x-api-key": "user-secret",
            "Origin": "http://127.0.0.1:8000",
            "Cookie": "a=b",
        },
    )
    assert response.status_code == 200, response.text
    headers = last_engine_headers(h, "/v1/chat/completions")
    assert headers["authorization"] == f"Bearer {h.sup.internal_key}"
    assert "x-api-key" not in headers and "origin" not in headers and "cookie" not in headers
    assert headers["host"].startswith("127.0.0.1:")
    assert headers["host"] == f"127.0.0.1:{h.sup.internal_port}"


def test_streaming_is_relayed_and_usage_captured(h_ready: EngineHarness):
    h = h_ready
    with h.client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": MODEL, "stream": True, **CHAT},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        request_id = response.headers["x-splash-request-id"]
        text = "".join(response.iter_text())
    assert "data: [DONE]" in text
    row = usage_row(h, request_id)
    assert row["stream"] is True and row["status"] == 200
    assert row["ttft_ms"] is not None and row["duration_ms"] >= row["ttft_ms"]
    assert row["prompt_tokens"] and row["completion_tokens"]
    assert row["prompt_ms"] is not None and row["predicted_ms"] is not None


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/responses", {"input": "hi", "max_output_tokens": 8}),
        ("/v1/messages", {"max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}),
        ("/v1/completions", {"prompt": "hi", "max_tokens": 8}),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
def test_usage_is_captured_for_each_shape(
    h_ready: EngineHarness, path: str, body: dict[str, Any], stream: bool
):
    h = h_ready
    response = h.client.post(path, json={"model": MODEL, "stream": stream, **body})
    assert response.status_code == 200, response.text
    row = usage_row(h, response.headers["x-splash-request-id"])
    assert row["endpoint"] == path and row["stream"] is stream
    assert row["prompt_tokens"], row
    assert row["completion_tokens"], row
    assert row["ttft_ms"] is not None, row


def test_engine_errors_pass_through_and_raise_alerts(h_ready: EngineHarness):
    h = h_ready
    h.fake("POST", "/_fake/mode", {"mode": "queue_full"})
    response = h.client.post("/v1/chat/completions", json={"model": MODEL, **CHAT})
    assert response.status_code == 503
    assert response.headers.get("retry-after")
    row = usage_row(h, response.headers["x-splash-request-id"])
    assert row["status"] == 503 and row["error_code"]
    alerts = h.client.get("/api/admin/alerts").json()["alerts"]
    assert any(a["condition"] == "queue_full" for a in alerts), alerts
    h.fake("POST", "/_fake/mode", {"mode": "capacity_exhausted"})
    h.client.post("/v1/chat/completions", json={"model": MODEL, **CHAT})
    alerts = h.client.get("/api/admin/alerts").json()["alerts"]
    assert any(a["condition"] == "capacity_exhausted" for a in alerts), alerts


def test_messages_errors_use_the_anthropic_shape(h_ready: EngineHarness):
    h = h_ready
    response = h.client.post(
        "/v1/messages",
        json={"model": "nobody/nothing", "max_tokens": 4, "messages": []},
    )
    assert response.status_code == 404
    assert response.json()["type"] == "error"
    assert response.json()["error"]["type"] == "not_found_error"


# --- Public-port checks -----------------------------------------------------------------


def test_host_allowlist(h_ready: EngineHarness):
    h = h_ready
    refused = h.client.get("/v1/models", headers={"Host": "evil.example"})
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "host_not_allowed"
    h.patch_settings({"global": {"server": {"allowed_hosts": ["mac.local"]}}})
    assert h.client.get("/v1/models", headers={"Host": "mac.local:8000"}).status_code == 200


def test_origin_check_and_preflight(h_ready: EngineHarness):
    h = h_ready
    origin = {"Origin": "tauri://localhost"}
    refused = h.client.get("/v1/models", headers=origin)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "origin_not_allowed"
    assert (
        h.client.get("/v1/models", headers={"Origin": "http://127.0.0.1:8000"}).status_code == 200
    )
    h.patch_settings({"global": {"server": {"allowed_origins": ["tauri://localhost"]}}})
    allowed = h.client.get("/v1/models", headers=origin)
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "tauri://localhost"
    pre = h.client.options(
        "/v1/chat/completions",
        headers={
            **origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, authorization",
        },
    )
    assert pre.status_code == 204
    assert "POST" in pre.headers["access-control-allow-methods"]
    assert pre.headers["access-control-allow-headers"] == "content-type, authorization"


def test_api_key_required(h_ready: EngineHarness):
    h = h_ready
    key = h.client.post("/api/admin/settings/secrets/api-key")
    assert key.status_code == 200, key.text
    secret = key.json()["key"]
    h.patch_settings({"global": {"security": {"api_key_required": True}}})
    # The CLI token (the harness client's default header) from loopback passes.
    assert h.client.get("/v1/models").status_code == 200
    cli_token = h.client.headers.pop("Authorization")
    try:
        anonymous = h.client.get("/v1/models")
        assert anonymous.status_code == 401
        assert anonymous.headers["www-authenticate"] == "Bearer"
        wrong = h.client.get("/v1/models", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        empty = h.client.get("/v1/models", headers={"Authorization": "", "x-api-key": secret})
        assert empty.status_code == 401, "every credential given must be the key, as in Splash"
        bearer = h.client.post(
            "/v1/chat/completions",
            json={"model": MODEL, **CHAT},
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert bearer.status_code == 200
        assert h.client.get("/v1/models", headers={"x-api-key": secret}).status_code == 200
        both = {"x-api-key": secret, "Authorization": f"Bearer {secret}"}
        assert h.client.get("/v1/models", headers=both).status_code == 200
        # /health and /ready stay public; /status follows the key.
        assert h.client.get("/health").status_code == 200
        assert h.client.get("/ready").status_code == 200
        assert h.client.get("/status").status_code == 401
        assert h.client.get("/metrics").status_code == 401
    finally:
        h.client.headers["Authorization"] = cli_token


# --- /v1/models (SPEC §7.3) ----------------------------------------------------------------


def ids(listing: dict[str, Any]) -> list[str]:
    return [entry["id"] for entry in listing["data"]]


def test_models_while_stopped_lists_installed_models_unloaded(harness_factory: H):
    h = harness_factory(installed=(MODEL, MODEL_27B))
    listing = h.client.get("/v1/models").json()
    assert ids(listing) == sorted([MODEL, MODEL_27B])
    assert all(entry["loaded"] is False for entry in listing["data"])
    assert all(entry["max_model_len"] == 262144 for entry in listing["data"])
    assert [m["name"] for m in listing["models"]] == ids(listing)


def test_models_order_active_profiles_then_installed(harness_factory: H):
    h = harness_factory(installed=(MODEL, MODEL_27B))
    model_settings(h, MODEL, profiles={"fast": {"temperature": 0.2}})
    h.load(MODEL)
    listing = h.client.get("/v1/models").json()
    got = ids(listing)
    assert got[0] == MODEL, "the active model comes first: Splash's launchers take data[0]"
    profiles = [i for i in got if i.startswith(f"{MODEL}:")]
    assert set(profiles) == {
        f"{MODEL}:no-think",
        f"{MODEL}:deterministic",
        f"{MODEL}:qwen-nonthinking",
        f"{MODEL}:fast",
    }
    assert f"{MODEL}:default" not in got
    assert got[-1] == MODEL_27B
    assert got.index(MODEL_27B) > max(got.index(p) for p in profiles)
    by_id = {entry["id"]: entry for entry in listing["data"]}
    assert by_id[MODEL]["loaded"] is True
    assert by_id[f"{MODEL}:fast"]["root"] == MODEL and by_id[f"{MODEL}:fast"]["profile"] == "fast"
    assert by_id[MODEL_27B]["loaded"] is False
    # The real context reported by the engine is remembered for unloaded listings.
    assert by_id[MODEL]["max_model_len"] == 262144
    one = h.client.get(f"/v1/models/{MODEL}:fast")
    assert one.status_code == 200 and one.json()["root"] == MODEL
    assert h.client.get("/v1/models/nobody/nothing").status_code == 404


def test_models_lists_aliases_first_when_announced(harness_factory: H):
    h = harness_factory()
    model_settings(
        h, MODEL, serve={"served_model_names": ["qwen-moe"], "announce_served_name": True}
    )
    h.load(MODEL)
    listing = h.client.get("/v1/models").json()
    assert ids(listing)[0] == "qwen-moe", ids(listing)
    assert MODEL in ids(listing)


def test_models_without_auto_load_lists_only_the_active_model(harness_factory: H):
    h = harness_factory(installed=(MODEL, MODEL_27B))
    h.patch_settings({"global": {"routing": {"auto_load": False}}})
    assert ids(h.client.get("/v1/models").json()) == []
    h.load(MODEL)
    got = ids(h.client.get("/v1/models").json())
    assert MODEL in got and MODEL_27B not in got


# --- Routing and auto-load (SPEC §7.4) ------------------------------------------------------


def test_alias_is_forwarded_unchanged(harness_factory: H):
    h = harness_factory()
    model_settings(h, MODEL, serve={"served_model_names": ["qwen-moe"]})
    h.load(MODEL)
    assert (
        h.client.post("/v1/chat/completions", json={"model": "qwen-moe", **CHAT}).status_code == 200
    )
    assert last_body(h, "/v1/chat/completions")["model"] == "qwen-moe"
    profile = h.client.post("/v1/chat/completions", json={"model": "qwen-moe:no-think", **CHAT})
    assert profile.status_code == 200
    sent = last_body(h, "/v1/chat/completions")
    assert sent["model"] == MODEL and sent["reasoning_effort"] == "none"


def test_unknown_model_is_404_listing_installed(h_ready: EngineHarness):
    response = h_ready.client.post("/v1/chat/completions", json={"model": "x/y", **CHAT})
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "model_not_found" and error["details"]["installed"] == [MODEL]


def test_unknown_profile_is_404(h_ready: EngineHarness):
    response = h_ready.client.post("/v1/chat/completions", json={"model": f"{MODEL}:nope", **CHAT})
    assert response.status_code == 404


def test_empty_model_goes_to_the_active_model(h_ready: EngineHarness):
    h = h_ready
    assert h.client.post("/v1/chat/completions", json=CHAT).status_code == 200
    assert "model" not in last_body(h, "/v1/chat/completions")


def test_unknown_model_fallback(h_ready: EngineHarness):
    h = h_ready
    h.patch_settings({"global": {"routing": {"unknown_model_fallback": True}}})
    response = h.client.post("/v1/chat/completions", json={"model": "gpt-4o", **CHAT})
    assert response.status_code == 200, response.text
    assert last_body(h, "/v1/chat/completions")["model"] == MODEL


def test_stopped_engine_auto_loads_the_requested_model(harness_factory: H):
    h = harness_factory()
    response = h.client.post("/v1/chat/completions", json={"model": f"{MODEL}:no-think", **CHAT})
    assert response.status_code == 200, response.text
    assert h.engine()["state"] in ("ready", "busy") and h.engine()["model"] == MODEL
    assert last_body(h, "/v1/chat/completions")["reasoning_effort"] == "none"
    sessions = h.state.usage.sessions()
    assert sessions[0]["model"] == MODEL


def test_stopped_engine_loads_the_default_model_for_an_unknown_name(harness_factory: H):
    h = harness_factory()
    h.patch_settings(
        {"global": {"routing": {"default_model": MODEL, "unknown_model_fallback": True}}}
    )
    response = h.client.post("/v1/chat/completions", json={"model": "gpt-4o", **CHAT})
    assert response.status_code == 200, response.text
    assert h.engine()["model"] == MODEL


def test_stopped_engine_without_a_model_name_is_503(harness_factory: H):
    h = harness_factory()
    response = h.client.post("/v1/chat/completions", json=CHAT)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "engine_unavailable"
    assert response.headers["retry-after"] == "5"


def test_auto_load_off_refuses_to_start(harness_factory: H):
    h = harness_factory()
    h.patch_settings({"global": {"routing": {"auto_load": False}}})
    response = h.client.post("/v1/chat/completions", json={"model": MODEL, **CHAT})
    assert response.status_code == 503
    assert h.engine()["state"] == "stopped"


def test_idle_engine_switches_models_for_a_request(harness_factory: H):
    h = harness_factory(installed=(MODEL, MODEL_27B))
    h.load(MODEL)
    response = h.client.post("/v1/chat/completions", json={"model": MODEL_27B, **CHAT})
    assert response.status_code == 200, response.text
    assert h.engine()["model"] == MODEL_27B
    assert last_body(h, "/v1/chat/completions")["model"] == MODEL_27B


def _slow_request(h: EngineHarness, done: threading.Event) -> None:
    body = {"model": MODEL, "stream": True, "max_tokens": 120, "ignore_eos": True}
    body["messages"] = [{"role": "user", "content": "go"}]
    h.client.post("/v1/chat/completions", json=body)
    done.set()


def _start_busy(h: EngineHarness) -> tuple[threading.Thread, threading.Event]:
    """One long request in flight (the TestClient buffers bodies, so poll the engine)."""
    done = threading.Event()
    worker = threading.Thread(target=_slow_request, args=(h, done), daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while h.engine()["requests_in_flight"] == 0:
        assert time.monotonic() < deadline, "the slow request never started"
        time.sleep(0.02)
    return worker, done


def test_busy_engine_refuses_to_switch_with_retry_after(harness_factory: H):
    h = harness_factory(installed=(MODEL, MODEL_27B), env={"FAKE_SPLASH_TOKS": "40"})
    h.load(MODEL)
    worker, _ = _start_busy(h)
    response = h.client.post("/v1/chat/completions", json={"model": MODEL_27B, **CHAT})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "10"
    error = response.json()["error"]
    assert error["code"] == "model_switch_busy"
    assert f"serving {MODEL}" in error["message"]
    assert h.engine()["model"] == MODEL, "nothing switched"
    worker.join(30)


def test_busy_engine_waits_with_the_wait_policy(harness_factory: H):
    h = harness_factory(installed=(MODEL, MODEL_27B), env={"FAKE_SPLASH_TOKS": "60"})
    h.load(MODEL)
    worker, done = _start_busy(h)
    response = h.client.post(
        "/v1/chat/completions",
        json={"model": MODEL_27B, **CHAT},
        headers={"X-Splash-Switch": "wait"},
    )
    assert response.status_code == 200, response.text
    assert done.is_set(), "the switch waited for the in-flight request"
    assert h.engine()["model"] == MODEL_27B
    worker.join(30)


def test_wait_policy_setting(harness_factory: H):
    h = harness_factory(installed=(MODEL, MODEL_27B), env={"FAKE_SPLASH_TOKS": "60"})
    h.patch_settings({"global": {"routing": {"switch_when_busy": "wait"}}})
    h.load(MODEL)
    worker, done = _start_busy(h)
    response = h.client.post("/v1/chat/completions", json={"model": MODEL_27B, **CHAT})
    assert response.status_code == 200, response.text
    assert done.is_set() and h.engine()["model"] == MODEL_27B
    worker.join(30)


# --- /ready, /status, /metrics -----------------------------------------------------------------


def test_ready_status_and_metrics(harness_factory: H):
    h = harness_factory()
    assert h.client.get("/ready").status_code == 503
    assert h.client.get("/status").status_code == 503
    metrics = h.client.get("/metrics")
    assert metrics.status_code == 200 and "splash_gui_up 1" in metrics.text
    assert 'splash_gui_engine_state{state="stopped"} 1' in metrics.text
    h.load()
    assert h.client.get("/ready").status_code == 200
    status = h.client.get("/status")
    assert status.status_code == 200 and status.json()["schema_version"] == 6
    text = h.client.get("/metrics").text
    assert 'splash_gui_engine_state{state="ready"} 1' in text
    assert "splash_" in text.split("splash_gui_up")[0], "the engine's own metrics come first"


def test_responses_get_and_delete_pass_through(h_ready: EngineHarness):
    h = h_ready
    created = h.client.post(
        "/v1/responses", json={"model": MODEL, "input": "hi", "store": True, "max_output_tokens": 4}
    )
    assert created.status_code == 200, created.text
    rid = created.json()["id"]
    assert h.client.get(f"/v1/responses/{rid}").json()["id"] == rid
    assert h.client.delete(f"/v1/responses/{rid}").json()["deleted"] is True
    assert h.client.get(f"/v1/responses/{rid}").status_code == 404


def test_unknown_v1_route_is_404(h_ready: EngineHarness):
    assert h_ready.client.get("/v1/nothing").status_code == 404


def _disconnect_after_first_chunk(h: EngineHarness, body: dict[str, Any]) -> str:
    """Drive the ASGI app directly and hang up after the first body chunk, as a
    client closing its stream does (the TestClient cannot disconnect mid-body)."""
    import anyio

    raw = json.dumps(body).encode()
    token = h.state.auth.cli_token()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1:8000"),
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {token}".encode()),
        ],
        "client": ("127.0.0.1", 50001),
        "server": ("127.0.0.1", 8000),
        "state": {},
    }
    headers: dict[str, str] = {}

    async def drive() -> None:
        hung_up = anyio.Event()
        sent_body = False

        async def receive() -> dict[str, Any]:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": raw, "more_body": False}
            await hung_up.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers.update({k.decode(): v.decode() for k, v in message["headers"]})
            elif message["type"] == "http.response.body" and message.get("body"):
                hung_up.set()

        with anyio.fail_after(20):
            await h.app(scope, receive, send)  # type: ignore[arg-type]

    assert h.client.portal is not None
    h.client.portal.call(drive)
    return headers["x-splash-request-id"]


def test_a_client_that_disconnects_is_recorded_as_cancelled(harness_factory: H):
    h = harness_factory(env={"FAKE_SPLASH_TOKS": "40"})
    h.load()
    body = {"model": MODEL, "stream": True, "max_tokens": 400, "ignore_eos": True}
    body["messages"] = [{"role": "user", "content": "go"}]
    request_id = _disconnect_after_first_chunk(h, body)
    row = usage_row(h, request_id)
    assert row["status"] == 499 and row["error_code"] == "cancelled", row
    cancelled = h.client.get("/api/admin/usage/requests", params={"status": "cancelled"}).json()
    assert cancelled["total"] == 1
    summary = h.client.get("/api/admin/usage/summary").json()
    assert summary["cancelled"] == 1 and summary["failed"] == 0
    deadline = time.monotonic() + 5
    while h.engine()["requests_in_flight"]:
        assert time.monotonic() < deadline, "the in-flight count leaked"
        time.sleep(0.02)


def test_json_body_errors(h_ready: EngineHarness):
    h = h_ready
    bad = h.client.post(
        "/v1/chat/completions", content=b"{nope", headers={"content-type": "application/json"}
    )
    assert bad.status_code == 400
    array = h.client.post("/v1/chat/completions", content=json.dumps([1]).encode())
    assert array.status_code == 400


@pytest.fixture
def h_ready(harness_factory: H) -> EngineHarness:
    h = harness_factory()
    view = h.load()
    assert view["state"] == "ready", view
    return h


def test_only_the_request_gate_counts_as_queue_full(h_ready: EngineHarness) -> None:
    pipeline = h_ready.state.proxy
    alerts = h_ready.state.alerts
    pipeline._alerts(503, "frontend_overloaded", "request body capacity is exhausted")
    assert not alerts.active("queue_full")
    pipeline._alerts(503, "overloaded_error", "frontend request capacity is exhausted")
    assert alerts.active("queue_full"), "Anthropic-shaped errors carry no code"


def test_the_last_known_context_is_listed_for_an_unloaded_model(harness_factory: H) -> None:
    h = harness_factory(env={"FAKE_SPLASH_AUTO_CONTEXT": "100000"})
    view = h.load()
    assert view["maximum_context_tokens"] == 100000
    h.client.post("/api/admin/engine/stop")
    h.wait_state("stopped")
    entry = h.client.get(f"/v1/models/{MODEL}").json()
    assert entry["loaded"] is False and entry["max_model_len"] == 100000


# --- Playground "raw to engine" (SPEC §10.6) ----------------------------------------------


def test_raw_to_engine_skips_injection_but_uses_the_internal_key(h_ready: EngineHarness) -> None:
    h = h_ready
    model_settings(h, MODEL, sampling_defaults={"temperature": 0.2})
    response = h.client.post(
        "/api/admin/engine/raw/v1/chat/completions", json={"model": MODEL, **CHAT}
    )
    assert response.status_code == 200, response.text
    sent = last_body(h, "/v1/chat/completions")
    assert "temperature" not in sent, "raw mode never injects defaults"
    assert last_engine_headers(h, "/v1/chat/completions")["authorization"] == (
        f"Bearer {h.sup.internal_key}"
    )
    assert h.client.get("/api/admin/usage/summary").json()["requests"] == 0, "no usage row"
    created = h.client.post(
        "/api/admin/engine/raw/v1/responses",
        json={"model": MODEL, "input": "hi", "store": True, "max_output_tokens": 4},
    ).json()
    got = h.client.get(f"/api/admin/engine/raw/v1/responses/{created['id']}")
    assert got.status_code == 200 and got.json()["id"] == created["id"]
    assert h.client.delete(f"/api/admin/engine/raw/v1/responses/{created['id']}").status_code == 200


def test_raw_to_engine_while_stopped(harness_factory: H) -> None:
    h = harness_factory()
    response = h.client.post("/api/admin/engine/raw/tokenize", json={"content": "x"})
    assert response.status_code == 503 and response.headers["retry-after"] == "5"


# --- Splash 1.3.0: retryable overloads and each API's error format ------------------------

SYSTEMONE = {"model": MODEL, "state": "s", "questions": {"q": {"type": "noul"}}}


def test_engine_overload_keeps_its_retry_after_in_each_apis_format(h_ready: EngineHarness):
    """1.3.0 answers an overload with a retryable 503 + Retry-After: 1 (529 on
    /v1/systemone), in the requested API's format; the proxy passes all of it on."""
    h = h_ready
    h.fake("POST", "/_fake/mode", {"mode": "queue_full"})
    chat = h.client.post("/v1/chat/completions", json={"model": MODEL, **CHAT})
    assert (chat.status_code, chat.headers["retry-after"]) == (503, "1")
    assert chat.json() == {
        "error": {
            "message": "frontend request capacity is exhausted",
            "type": "server_error",
            "code": "frontend_overloaded",
        }
    }
    messages = h.client.post(
        "/v1/messages",
        json={"model": MODEL, "max_tokens": 4, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert (messages.status_code, messages.headers["retry-after"]) == (503, "1")
    assert messages.json() == {
        "type": "error",
        "error": {"type": "overloaded_error", "message": "frontend request capacity is exhausted"},
    }
    systemone = h.client.post("/v1/systemone", json=SYSTEMONE)
    assert (systemone.status_code, systemone.headers["retry-after"]) == (529, "1")


def test_the_managers_own_systemone_errors_answer_as_splash_does(harness_factory: H) -> None:
    h = harness_factory()  # installed, not loaded, auto-load off: the engine is stopped
    h.patch_settings({"global": {"routing": {"auto_load": False}}})
    stopped = h.client.post("/v1/systemone", json=SYSTEMONE)
    assert stopped.status_code == 529 and stopped.headers["retry-after"] == "5"
    assert stopped.json()["error"]["code"] == "engine_unavailable"
    invalid = h.client.post(
        "/v1/systemone", content=b"{nope", headers={"content-type": "application/json"}
    )
    assert invalid.status_code == 422
    assert invalid.json() == {
        "detail": [{"loc": ["body"], "msg": "invalid JSON request body", "type": "value_error"}]
    }
    chat = h.client.post(
        "/v1/chat/completions", content=b"[]", headers={"content-type": "application/json"}
    )
    assert chat.status_code == 400
    assert chat.json()["error"]["message"] == "request body must be an object"

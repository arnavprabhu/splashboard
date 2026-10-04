"""Auth, Host checks and every simulated failure mode."""

from __future__ import annotations

import http.client
import json
import re
import threading
import time

import pytest

from conftest import chat_body, sse

MODEL = "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"


@pytest.fixture(scope="module")
def keyed(tmp_path_factory):
    from harness import FakeSplash

    with FakeSplash(tmp_path_factory.mktemp("keyed"), api_key="sekret") as fake:
        yield fake


def test_auth_required_except_public_routes(keyed):
    assert keyed.json("GET", "/health", auth=False)[0] == 200
    assert keyed.json("GET", "/ready", auth=False)[0] == 200
    status, headers, body = keyed.request("GET", "/status", auth=False)
    assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
    assert json.loads(body) == {
        "error": {
            "message": "invalid or missing API key",
            "type": "invalid_request_error",
            "code": "authentication_error",
        }
    }
    assert keyed.json("GET", "/metrics", auth=False)[0] == 401
    assert keyed.json("GET", "/status", auth=False, headers={"Authorization": "Bearer wrong"})[0] == 401
    assert keyed.json("GET", "/status")[0] == 200
    assert keyed.json("GET", "/v1/models", auth=False, headers={"x-api-key": "sekret"})[0] == 200


def test_auth_error_anthropic_shape(keyed):
    status, error = keyed.json(
        "POST",
        "/v1/messages",
        {"model": MODEL, "max_tokens": 4, "messages": [{"role": "user", "content": "x"}]},
        auth=False,
    )
    assert status == 401
    assert error == {
        "type": "error",
        "error": {"type": "authentication_error", "message": "invalid or missing API key"},
    }


def test_host_header_checked(keyed):
    connection = http.client.HTTPConnection("127.0.0.1", keyed.port, timeout=5)
    connection.request("GET", "/health", headers={"Host": "evil.example"})
    response = connection.getresponse()
    assert response.status == 403
    assert json.loads(response.read())["error"]["message"] == (
        "Host evil.example is not allowed; restart the server with --allowed-host evil.example to accept it"
    )


def test_engine_recovering(make_engine):
    fake = make_engine()
    fake.set_mode("engine_recovering")
    status, headers, body = fake.request("POST", "/v1/chat/completions", chat_body())
    assert status == 503 and headers["Retry-After"] == "1"
    error = json.loads(body)["error"]
    assert error["code"] == "engine_recovering" and error["type"] == "server_error"
    assert error["message"].startswith("engine is recovering; retry shortly (last failure: ")
    transport = fake.json("GET", "/status")[1]["transport"]
    assert transport["recovering"] is True and transport["stopped"] is False and transport["error"]
    assert fake.json("GET", "/ready")[0] == 503
    # Prompt-only endpoints are not refused.
    assert fake.json("POST", "/tokenize", {"content": "x"})[0] == 200
    fake.wait_for_line("Engine failed · ", stream="stderr")
    fake.set_mode("normal")
    fake.wait_for_line("Engine restarted", stream="stdout")
    status_body = fake.json("GET", "/status")[1]
    assert status_body["transport"]["restarts"] == 1 and status_body["ready"] is True


def test_engine_failed(make_engine):
    fake = make_engine()
    fake.set_mode("engine_failed")
    status, error = fake.json("POST", "/v1/chat/completions", chat_body())
    assert status == 500 and error["error"]["code"] == "engine_failed"
    assert "Splash stopped restarting it" in error["error"]["message"]
    transport = fake.json("GET", "/status")[1]["transport"]
    assert transport["stopped"] is True and transport["recovering"] is False
    fake.wait_for_line("Engine stopped · ", stream="stderr")
    status, error = fake.json(
        "POST", "/v1/messages", {"model": MODEL, "max_tokens": 4, "messages": [{"role": "user", "content": "x"}]}
    )
    assert status == 500 and error["error"]["type"] == "api_error"


def test_queue_full_mode_and_real_queue_size(make_engine):
    fake = make_engine(args=["--no-webui", "--queue-size", "1"], env={"FAKE_SPLASH_TOKS": "20"})
    result = {}

    def slow():
        result["slow"] = fake.json("POST", "/v1/chat/completions", chat_body("slow one"))[0]

    worker = threading.Thread(target=slow)
    worker.start()
    time.sleep(0.3)
    status, error = fake.json("POST", "/v1/chat/completions", chat_body())
    assert status == 503 and error["error"]["code"] == "frontend_overloaded"
    assert error["error"]["message"] == "frontend request capacity is exhausted"
    worker.join()
    assert result["slow"] == 200
    fake.set_mode("queue_full", tokens_per_second=2000.0)
    assert fake.json("POST", "/v1/chat/completions", chat_body())[0] == 503
    assert (
        fake.json("POST", "/v1/systemone", {"model": MODEL, "state": "s", "questions": {"q": {"type": "noul"}}})[0]
        == 529
    )


def test_capacity_exhausted_and_resource_timeout(make_engine):
    fake = make_engine()
    fake.set_mode("capacity_exhausted")
    status, error = fake.json("POST", "/v1/chat/completions", chat_body())
    assert status == 400 and error["error"]["code"] == "capacity_exhausted"
    assert error["error"]["message"].startswith("the request does not fit in the memory this server may use")
    status_body = fake.json("GET", "/status")[1]
    assert status_body["metrics"]["capacity_failures"] == 1 and status_body["requests"]["failed"] == 1
    fake.set_mode("resource_timeout")
    status, error = fake.json("POST", "/v1/chat/completions", chat_body())
    assert status == 503 and error["error"]["code"] == "resource_timeout"


def test_fail_midstream_sends_error_events(make_engine):
    fake = make_engine()
    fake.set_mode("fail_midstream")
    events = sse(fake, "/v1/chat/completions", chat_body("long enough reply", stream=True))
    assert events[-1] == (None, "[DONE]")
    assert events[-2][1]["error"]["code"] == "runtime_unavailable"
    events = sse(fake, "/v1/responses", {"input": "x", "stream": True})
    assert events[-1][0] == "response.failed"
    assert events[-1][1]["response"]["error"]["code"] == "runtime_unavailable"
    events = sse(
        fake,
        "/v1/messages",
        {"model": MODEL, "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "x y z"}]},
    )
    assert events[-1][0] == "error" and events[-1][1]["error"]["type"] == "overloaded_error"


def test_critical_memory_pressure(make_engine):
    fake = make_engine()
    fake.set_mode(memory_pressure="critical")
    assert fake.json("GET", "/ready") == (503, {"status": "unavailable"})
    status_body = fake.json("GET", "/status")[1]
    assert status_body["memory_pressure"] == "critical" and status_body["ready"] is False
    _, _, metrics = fake.request("GET", "/metrics")
    assert 'splash_memory_pressure{state="critical"} 1' in metrics.decode()
    fake.set_mode(memory_pressure="normal")
    assert fake.json("GET", "/ready")[0] == 200


def test_request_timeout_504(make_engine):
    fake = make_engine(env={"FAKE_SPLASH_TOKS": "10"})
    status, error = fake.json("POST", "/v1/chat/completions", chat_body(timeout=0.2))
    assert status == 504 and error["error"]["code"] == "request_timeout"


def test_keepalive_during_slow_prefill(make_engine):
    fake = make_engine(env={"FAKE_SPLASH_PREFILL_TPS": "100", "FAKE_SPLASH_KEEPALIVE_SECONDS": "0.1"})
    prompt = " ".join(["w"] * 60)
    events = sse(fake, "/v1/chat/completions", chat_body(prompt, stream=True, reasoning_effort="none"))
    data = [d for _, d in events if isinstance(d, dict)]
    empty = [
        d for d in data if d["choices"] and d["choices"][0]["delta"] == {} and not d["choices"][0]["finish_reason"]
    ]
    assert empty, "expected empty keepalive chunks after the start"
    other = " ".join(["v"] * 60)
    events = sse(
        fake,
        "/v1/messages",
        {"model": MODEL, "max_tokens": 20, "stream": True, "messages": [{"role": "user", "content": other}]},
    )
    assert "ping" in [e for e, _ in events]


def test_crash_exits_nonzero(make_engine):
    fake = make_engine()
    fake.crash(code=3)
    assert fake.process.wait(5) == 3
    fake.wait_for_line("native transport stopped after an engine failure", stream="stderr")


def test_idle_release_and_restore(make_engine):
    fake = make_engine(env={"FAKE_SPLASH_IDLE_RELEASE_SECONDS": "0.3", "FAKE_SPLASH_RESTORE_SECONDS": "0.05"})
    fake.wait_for_line(r"^Weights released after 0\.3 s without a request; the next request restores them$", 5)
    assert fake.json("POST", "/v1/chat/completions", chat_body())[0] == 200
    fake.wait_for_line(r"^Weights restored in \d+\.\d\d s$", 5)


def test_request_log_lines(make_engine):
    """server/diagnostics.py print_request: Done (with the tools signature),
    Cancelled when the client leaves, Error for a failed request."""
    fake = make_engine(env={"FAKE_SPLASH_TOKS": "40"})
    tool = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    assert (
        fake.json(
            "POST", "/v1/chat/completions", chat_body("lookup x", tools=[tool], tool_choice="required", max_tokens=8)
        )[0]
        == 200
    )
    done = fake.wait_for_line(r"Done · input \d", stream="stdout")
    assert re.search(r" · cached \d+ · output \d+ · tools 1·[0-9a-f]{8} · TTFT \d+\.\ds · \d+\.\d tok/s$", done)
    connection = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=10)
    connection.request(
        "POST",
        "/v1/chat/completions",
        json.dumps(chat_body("a long one", stream=True, max_tokens=400)),
        {"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    response.read1(64)
    response.close()
    connection.close()
    fake.wait_for_line(r"Cancelled · input \d+ · cached \d+ · output \d+$", stream="stdout")
    fake.set_mode("capacity_exhausted")
    assert fake.json("POST", "/v1/chat/completions", chat_body())[0] == 400
    fake.wait_for_line(r"^\d\d:\d\d:\d\d Error · capacity_exhausted$", stream="stderr")
    fake.wait_for_line(r"^\d\d:\d\d:\d\d Error · capacity_exhausted · POST /v1/chat/completions$", stream="stderr")

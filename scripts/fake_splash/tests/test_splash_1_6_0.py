"""Splash 1.6.0 in the fake: /status schema 8 with the memory ledger, the elastic
expert store and the first-token breakdown, a Chat request's token count, the limits
on stop sequences, a quarantined request, the context the server serves now, and the
installer's own account of a failed download, which the launcher relays once."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from test_installer import install

from conftest import chat_body
from harness import PKG, SPLASH_BIN, FakeSplash, fake_env

DENSE = "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M"
MOE = "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"
FLASH_NEXT = "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF:Q2_0"


def test_release_is_1_6_0():
    assert json.loads((PKG / "release.json").read_text())["version"] == "1.6.0"


def test_status_is_schema_8_with_the_memory_ledger(tmp_path):
    with FakeSplash(tmp_path, model=DENSE) as engine:
        assert engine.json("POST", "/v1/chat/completions", chat_body(max_tokens=8))[0] == 200
        status = engine.json("GET", "/status")[1]
        assert status["schema_version"] == 8
        governor = status["memory_governor"]
        assert governor["claims"] == {"waiting": 0, "bytes": 0, "oldest_ms": 0.0}
        assert [account["name"] for account in governor["accounts"]] == ["kv"]
        assert set(governor["owed"]) == {"transfers", "bytes", "to_claims_bytes", "oldest_ms"}
        # The context the server serves now.
        assert status["capabilities"] == {"max_context_tokens": status["maximum_context_tokens"]}
        # Where a first token's time went.
        parts = status["metrics"]["ttft_parts_ms"]
        assert set(parts) == {"own_prefill", "claim_wait", "decode", "other_prefill", "rest"}
        assert parts["own_prefill"]["samples"] == 1 and parts["claim_wait"]["samples"] == 0
        assert "prefill_ms" in status["metrics"]["decode_gap_causes"]
        # The two fields schema 8 removed.
        assert "resident_kv_bytes" not in status["memory_plan"]["budget"]
        engine.set_mode(memory_claims_waiting=2)
        claims = engine.json("GET", "/status")[1]["memory_governor"]["claims"]
        assert claims["waiting"] == 2 and claims["bytes"] > 0 and claims["oldest_ms"] > 0


def test_the_expert_store_is_elastic_under_auto_and_fixed_by_a_size(tmp_path):
    with FakeSplash(tmp_path / "auto", model=FLASH_NEXT) as engine:
        experts = engine.json("GET", "/status")[1]["streamed_experts"]
        assert experts["chunks"] == experts["cap_chunks"] and experts["floor_bytes"] < experts["cap_bytes"]
        assert experts["shrinks"] == 0
        engine.set_mode(expert_store_shrunk=True)
        status = engine.json("GET", "/status")[1]
        shrunk = status["streamed_experts"]
        assert shrunk["chunks"] < shrunk["cap_chunks"] and shrunk["shrinks"] == 1
        assert shrunk["slots"] < experts["slots"]
        names = [account["name"] for account in status["memory_governor"]["accounts"]]
        assert names == ["kv", "experts"]
        assert "contended_commands" not in status["kv_spill"]["history_stream"]
    args = ("--no-webui", "--expert-memory", "4G")
    with FakeSplash(tmp_path / "fixed", model=FLASH_NEXT, args=args) as engine:
        engine.set_mode(expert_store_shrunk=True)
        fixed = engine.json("GET", "/status")[1]["streamed_experts"]
        assert fixed["floor_bytes"] == fixed["cap_bytes"] and fixed["chunks"] == fixed["cap_chunks"]
        assert fixed["shrinks"] == 0


def test_a_chat_request_s_tokens_are_counted_without_inference(tmp_path):
    with FakeSplash(tmp_path, model=MOE) as engine:
        before = engine.json("GET", "/status")[1]["requests"]["submitted"]
        status, body = engine.json("POST", "/v1/chat/completions/count_tokens", chat_body("count these tokens"))
        assert status == 200 and set(body) == {"input_tokens"} and body["input_tokens"] > 0
        longer = engine.json("POST", "/v1/chat/completions/count_tokens", chat_body("count these tokens " * 20))[1]
        assert longer["input_tokens"] > body["input_tokens"]
        assert engine.json("GET", "/status")[1]["requests"]["submitted"] == before
        # A prompt-only route: it answers while the engine recovers, as /tokenize does.
        engine.set_mode("engine_recovering")
        assert engine.json("POST", "/v1/chat/completions/count_tokens", chat_body())[0] == 200


def test_stop_sequences_have_splash_s_limits_on_every_api(tmp_path):
    with FakeSplash(tmp_path, model=MOE) as engine:
        post = lambda path, body: engine.json("POST", path, body)  # noqa: E731
        status, body = post("/v1/chat/completions", chat_body(stop=["a", "b", "c", "d", "e"]))
        assert status == 400 and body["error"]["message"] == "stop must be up to four strings"
        status, body = post("/v1/chat/completions", chat_body(stop=["x" * 16385]))
        assert status == 400 and body["error"]["message"] == "each of stop must be at most 16384 characters"
        assert post("/v1/chat/completions", chat_body(stop=["x" * 16384], max_tokens=4))[0] == 200
        messages = {"model": MOE, "max_tokens": 4, "messages": [{"role": "user", "content": "hi"}]}
        # Messages: an array of strings that each hold a character other than whitespace.
        status, body = post("/v1/messages", {**messages, "stop_sequences": "END"})
        assert status == 400 and body["error"]["message"] == "stop_sequences must be an array of strings"
        status, body = post("/v1/messages", {**messages, "stop_sequences": ["END", "  "]})
        assert status == 400 and body["error"]["message"] == "each of stop_sequences must contain non-whitespace"
        status, body = post("/v1/messages", {**messages, "stop_sequences": ["a", "b", "c", "d", "e"]})
        assert status == 400 and body["error"]["message"] == "stop_sequences must be up to four strings"
        assert post("/v1/messages", {**messages, "stop_sequences": ["END"]})[0] == 200


def test_a_request_ended_by_a_stop_sequence_counts_as_completed(tmp_path):
    with FakeSplash(tmp_path, model=MOE, env={"FAKE_SPLASH_TEXT": "one two STOP three"}) as engine:
        status, body = engine.json("POST", "/v1/chat/completions", chat_body(stop=["STOP"], max_tokens=32))
        assert status == 200 and body["choices"][0]["finish_reason"] == "stop"
        assert "three" not in (body["choices"][0]["message"]["content"] or "")
        requests = engine.json("GET", "/status")[1]["requests"]
        assert requests["completed"] == 1 and requests["cancelled"] == 0


def test_a_quarantined_request_is_refused_with_400(tmp_path):
    with FakeSplash(tmp_path, model=MOE) as engine:
        engine.set_mode("request_quarantined")
        status, body = engine.json("POST", "/v1/chat/completions", chat_body())
        assert status == 400 and body["error"]["code"] == "request_quarantined"
        assert "two engine failures in a row" in body["error"]["message"]
        engine.set_mode("normal")
        assert engine.json("POST", "/v1/chat/completions", chat_body(max_tokens=4))[0] == 200


def test_the_installer_says_why_as_an_event_beside_its_error_line(tmp_path):
    code, out, err = install(tmp_path, "--model", MOE, "prepare", env={"FAKE_SPLASH_DL_FAIL": "gated"})
    assert code == 1 and err.startswith("error: cannot resolve unsloth/Qwen3.6-35B-A3B-GGUF: 401")
    event = json.loads(out.strip().splitlines()[-1])
    assert event["event"] == "error" and event["kind"] == "auth"
    assert err.strip() == "error: " + event["message"]
    code, out, _ = install(tmp_path / "net", "--model", MOE, "prepare", env={"FAKE_SPLASH_DL_FAIL": "network"})
    assert code == 1 and json.loads(out.strip().splitlines()[-1])["kind"] == "offline"
    # A refusal that is no Hub failure has no kind; verify prints no event.
    code, out, err = install(tmp_path / "room", "--model", MOE, "prepare", env={"FAKE_SPLASH_DL_FAIL": "no_room"})
    event = json.loads(out.strip().splitlines()[-1])
    assert code == 1 and event["kind"] is None and "not enough disk space for" in event["message"]
    code, out, err = install(tmp_path / "verify", "--model", MOE, "verify")
    assert code == 1 and "is not installed in" in err and '"event"' not in out


def test_the_launcher_relays_the_installer_and_does_not_repeat_its_failure(tmp_path: Path):
    result = subprocess.run(
        [str(SPLASH_BIN), "serve", "--model", MOE, "--no-webui", "--port", "18999"],
        env=fake_env(tmp_path, {"FAKE_SPLASH_DL_FAIL": "gated"}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    errors = [line for line in result.stderr.splitlines() if line.startswith("error:")]
    assert len(errors) == 1 and errors[0].startswith("error: cannot resolve unsloth/Qwen3.6-35B-A3B-GGUF")
    # The event is the launcher's to read, not the terminal's.
    assert '"event"' not in result.stdout and "model download or verification failed" not in result.stderr

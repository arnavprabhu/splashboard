"""Behaviour Splash 1.3.0 added that the GUI reads: the `weights` and `ane_ffn`
/status blocks, --disable-ane and --idle-release, retryable 503s with Retry-After,
errors in each API's format, and the agent launchers' own --port (#311)."""

from __future__ import annotations

import socket
import time

from test_startup_and_signals import run_cli

from conftest import chat_body
from harness import free_port

MODEL_27B = "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M"
SPLIT_REASON = (
    "at share 0.32 for chunks of 524 rows or more, 2.2% from the GPU alone on the "
    "Neural Engine's part (set up as calibrated before in 2.0 s)"
)


def test_27b_splits_its_prefill_ffn_with_the_neural_engine(make_engine):
    fake = make_engine(model=MODEL_27B)
    status = fake.json("GET", "/status")[1]
    assert status["ane_ffn"] == {
        "state": "split",
        "share": 0.3235294118,
        "minimum_rows": 524,
        "reason": SPLIT_REASON,
        "split_commands": 1,
        "reruns": 0,
        "ane_ms": 1258.4,
        "evaluations": 64,
    }
    assert status["memory_plan"]["budget"]["ane_ffn_bytes"] == 229_703_680
    assert status["memory_plan"]["model"]["memory"]["ane_ffn_bytes"] == 229_703_680
    fake.wait_for_line(r"Neural Engine FFN split at share 0\.32 for chunks of 524 rows or more")
    # A 3,000-token prompt runs one 2,048-row chunk and a 952-row chunk, both split.
    assert fake.json("POST", "/v1/chat/completions", chat_body("word " * 3000))[0] == 200
    after = fake.json("GET", "/status")[1]["ane_ffn"]
    assert (after["split_commands"], after["evaluations"]) == (3, 192)


def test_disable_ane_and_the_moe_keep_the_ffn_on_the_gpu(make_engine, tmp_path):
    gpu = make_engine(model=MODEL_27B, args=["--no-webui", "--disable-ane"], base=tmp_path / "27b")
    assert gpu.json("GET", "/status")[1]["ane_ffn"] == {
        "state": "off",
        "share": 0,
        "minimum_rows": 0,
        "reason": "as given",
        "split_commands": 0,
        "reruns": 0,
        "ane_ms": 0.0,
        "evaluations": 0,
    }
    gpu.wait_for_line(r"The GPU runs the prefill FFN alone, as given\.")
    assert gpu.json("GET", "/status")[1]["memory_plan"]["budget"]["ane_ffn_bytes"] == 0
    moe = make_engine(base=tmp_path / "moe")
    ane = moe.json("GET", "/status")[1]["ane_ffn"]
    assert (ane["state"], ane["reason"]) == ("off", "the target has no dense FFN layers")


def test_weights_block_follows_idle_release(make_engine, tmp_path):
    default = make_engine(base=tmp_path / "default")
    assert default.json("GET", "/status")[1]["weights"] == {
        "idle_release_seconds": 600.0,
        "released": False,
        "restores": 0,
    }
    off = make_engine(args=["--no-webui", "--idle-release", "off"], base=tmp_path / "off")
    assert off.json("GET", "/status")[1]["weights"]["idle_release_seconds"] is None
    quick = make_engine(args=["--no-webui", "--idle-release", "0.3s"], base=tmp_path / "quick")
    quick.wait_for_line(r"Weights released after 0\.3 s without a request")
    assert quick.json("GET", "/status")[1]["weights"]["released"] is True
    assert quick.json("POST", "/v1/chat/completions", chat_body())[0] == 200
    assert quick.json("GET", "/status")[1]["weights"] == {
        "idle_release_seconds": 0.3,
        "released": False,
        "restores": 1,
    }


def test_overload_is_a_retryable_503_in_each_apis_format(make_engine):
    fake = make_engine()
    fake.set_mode("queue_full")
    status, headers, _ = fake.request("POST", "/v1/chat/completions", chat_body())
    assert status == 503 and headers["Retry-After"] == "1"
    status, headers, body = fake.request(
        "POST", "/v1/messages", {"model": fake.model, "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
    )
    assert status == 503 and headers["Retry-After"] == "1"
    assert body.startswith(b'{"type":"error","error":{"type":"overloaded_error"')
    status, headers, _ = fake.request(
        "POST", "/v1/systemone", {"model": fake.model, "state": "s", "questions": {"q": {"type": "noul"}}}
    )
    assert status == 529 and headers["Retry-After"] == "1"


def test_a_connection_past_every_slot_gets_the_canned_503(make_engine):
    fake = make_engine(args=["--no-webui", "--queue-size", "1"])
    # --queue-size 1 plus 64 control slots: hold all 65 open, each yet to send a request.
    held = []
    try:
        for _ in range(65):
            held.append(socket.create_connection(("127.0.0.1", fake.port), timeout=5))
        time.sleep(0.5)  # until the server has accepted every one
        status, headers, body = fake.request("GET", "/v1/models")
        assert status == 503 and headers["Retry-After"] == "1"
        assert body == (
            b'{"error":{"type":"server_error","code":"frontend_overloaded",'
            b'"message":"HTTP connection capacity is exhausted"}}'
        )
    finally:
        for sock in held:
            sock.close()


def test_agent_launcher_takes_its_own_port_and_passes_the_agents_after_the_separator(make_engine, tmp_path):
    fake = make_engine()
    result = run_cli(tmp_path, "opencode", "--port", str(fake.port), "--", "--port", "4096")
    assert result.returncode == 0, result.stderr
    launched = result.stdout.splitlines()[-1]
    assert f'"base_url": "http://127.0.0.1:{fake.port}"' in launched and '"args": ["--port", "4096"]' in launched
    port = free_port()
    missing = run_cli(tmp_path, "hermes", "--port", str(port))
    assert missing.returncode == 1
    assert missing.stderr == (
        f"error: No ready Splash server at http://127.0.0.1:{port}. "
        "Run 'splash serve --model <HF_REPO_ID>' in another terminal first. "
        "If --port was meant for hermes itself, put it after --.\n"
    )

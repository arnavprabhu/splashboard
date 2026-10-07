"""Startup output, serve flag validation, budget refusal and stop signals."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

from conftest import REAL_SPLASH, chat_body
from harness import SPLASH_BIN, fake_env, free_port

TS = r"^\d\d:\d\d:\d\d "
REAL_CLI = Path("/opt/homebrew/opt/splash/bin/splash")


def run_cli(tmp_path, *args, env=None, timeout=30):
    return subprocess.run(
        [str(SPLASH_BIN), *args], env=fake_env(tmp_path, env), capture_output=True, text=True, timeout=timeout
    )


def test_version_matches_real_wording(tmp_path):
    result = run_cli(tmp_path, "--version")
    assert result.returncode == 0 and result.stdout == "Splash 1.3.0\n"
    if REAL_CLI.exists():
        real = subprocess.run([str(REAL_CLI), "--version"], capture_output=True, text=True, timeout=30)
        assert real.stdout == result.stdout


def test_serve_help_lists_every_real_flag(tmp_path):
    flags = set(re.findall(r"--[a-z-]+", run_cli(tmp_path, "serve", "--help").stdout))
    for flag in (
        "--model",
        "--revision",
        "--draft-model",
        "--language-only",
        "--offline",
        "--port",
        "--host",
        "--served-model-name",
        "--announce-served-name",
        "--default-reasoning-effort",
        "--kv-format",
        "--max-memory",
        "--max-cache-disk",
        "--persistent-cache",
        "--cache-dir",
        "--max-context",
        "--decode-share",
        "--allowed-host",
        "--allowed-origin",
        "--max-request-size",
        "--max-image-pixels",
        "--request-timeout",
        "--queue-size",
        "--api-key",
        "--no-webui",
    ):
        assert flag in flags, flag
    if REAL_CLI.exists():
        real = subprocess.run([str(REAL_CLI), "serve", "--help"], capture_output=True, text=True, timeout=30)
        assert set(re.findall(r"--[a-z-]+", real.stdout)) <= flags | {"--help"}


@pytest.mark.parametrize(
    "args, message",
    [
        (["--persistent-cache"], "--persistent-cache needs --max-cache-disk"),
        (["--max-cache-disk", "5G", "--cache-dir", "/tmp/x"], "--cache-dir needs --persistent-cache"),
        (["--announce-served-name"], "--announce-served-name needs --served-model-name"),
        (["--max-context", "300K"], "must be 'auto' or a token count up to 256K, such as 100K"),
        (["--kv-format", "fp8"], "invalid choice: 'fp8'"),
    ],
)
def test_serve_flag_validation(tmp_path, args, message):
    result = run_cli(tmp_path, "serve", "--model", "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M", *args)
    assert result.returncode == 2 and message in result.stderr


def test_bad_model_id(tmp_path):
    result = run_cli(tmp_path, "serve", "--model", "qwen3.8-27B")
    assert result.returncode == 2
    assert "model must be a full Hugging Face repository ID (owner/repo)" in result.stderr


def test_startup_lines_in_order(make_engine):
    fake = make_engine(env={"FAKE_SPLASH_DISK_SUGGESTION": "1"})
    fake.wait_for_line("may not hold a", stream="stderr")
    out = fake.stdout
    assert out[0].startswith("Selected Qwen3.6-35B-A3B-UD-Q4_K_M.gguf from unsloth/Qwen3.6-35B-A3B-GGUF.")
    assert any(
        line.startswith("Installing unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M as Qwen3.6-35B-A3B (gguf);") for line in out
    )
    timestamped = [line for line in out if re.match(TS, line)]
    assert re.match(TS + r"Loading · unsloth/Qwen3\.6-35B-A3B-GGUF:UD-Q4_K_M$", timestamped[0])
    assert re.match(TS + r"Chat template · patched to render later system messages in place", timestamped[1])
    assert re.match(
        TS + rf"Ready · unsloth/Qwen3\.6-35B-A3B-GGUF:UD-Q4_K_M · context 256K · http://127\.0\.0\.1:{fake.port}$",
        timestamped[2],
    )
    err = fake.stderr
    assert re.match(TS + r"Weights loaded in \d+\.\d\d s\.$", err[0])
    assert re.match(TS + r"Kernel policy for GPU family \d+ with \d+ cores\.$", err[1])
    assert re.match(
        TS + r"The \d+ MiB this Mac had available at startup may not hold a 262144-token request; one "
        r"that runs out of memory is suspended and replays its prompt\. --max-cache-disk SIZE keeps "
        r"its progress and cached prefixes on SSD\.$",
        err[2],
    )


def test_second_start_reports_installed_and_language_only(tmp_path, make_engine):
    first = make_engine(base=tmp_path)
    first.stop()
    second = make_engine(base=tmp_path, args=["--no-webui", "--language-only", "--max-context", "64K"])
    assert any(line.startswith("Installing ") and "vision disabled" in line for line in second.stdout)
    assert second.wait_for_line(r"Ready · .* · context 64K · language only · http://")
    second.stop()
    third = make_engine(base=tmp_path)
    assert third.stdout[0].startswith("Splash model unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M is already installed in ")
    models = third.json("GET", "/v1/models")[1]["data"][0]
    assert models["vision"] is True


def test_language_only_models_entry(make_engine):
    fake = make_engine(args=["--no-webui", "--language-only"])
    entry = fake.json("GET", "/v1/models")[1]["data"][0]
    assert entry["vision"] is False and entry["input_modalities"] == ["text"]
    status, error = fake.json(
        "POST",
        "/v1/chat/completions",
        {
            "messages": [
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}
            ]
        },
    )
    assert status == 400
    assert error["error"]["message"] == (
        "image input is not supported: this model is serving without vision (started with --language-only)"
    )
    status, error = fake.json(
        "POST",
        "/v1/messages",
        {
            "model": "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERg=="},
                        }
                    ],
                }
            ],
        },
    )
    assert status == 400 and error["type"] == "error"
    assert error["error"]["message"].startswith("PDF input is not supported: this model is serving without vision")


def test_served_model_aliases(make_engine):
    fake = make_engine(args=["--no-webui", "--served-model-name", "local/fast", "--announce-served-name"])
    data = fake.json("GET", "/v1/models")[1]["data"]
    assert [m["id"] for m in data] == ["local/fast", "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"]
    assert "root" not in data[0] and data[1]["root"] == "local/fast"
    body = fake.json("POST", "/v1/chat/completions", chat_body(model="unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"))[1]
    assert body["model"] == "local/fast"
    assert fake.json("GET", "/status")[1]["instance"]["model"] == "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"


def test_budget_refusal_exits_with_breakdown(tmp_path):
    result = run_cli(
        tmp_path,
        "serve",
        "--model",
        "mlx-community/Qwen3.8-27B-4bit",
        "--port",
        str(free_port()),
        "--max-memory",
        "8G",
        env={"FAKE_SPLASH_LOAD_SECONDS": "0"},
    )
    assert result.returncode == 1
    err = result.stderr
    assert (
        "error: runtime bootstrap failed [resource_assembly]: hard budget cannot fit one lane's state and the KV runway"
        in err
    )
    assert "engine memory budget validation failed [kv_pool_does_not_fit]" in err
    for label in (
        "physical memory: ",
        "configured memory limit: 8589934592 bytes (8192.00 MiB)",
        "hard budget: ",
        "target weights: ",
        "minimum required: ",
        "deficit: ",
    ):
        assert label in err
    assert re.search(r"^memory_plan_json: \{\"schema_version\":2,\"valid\":false", err, re.MULTILINE)
    # runtime/main.mm printBootstrapError writes with writeStderrLine: no timestamp.
    assert re.search(r"^error: runtime bootstrap failed", err, re.MULTILINE)
    assert re.search(TS + r"Error · native protocol reached EOF$", err, re.MULTILINE)
    assert " Ready · " not in result.stdout
    # server.py main(): the backend exists once the chat template is read, so
    # its cleanup still runs after a failed start.
    assert re.search(TS + r"Stopping · releasing engine resources$", result.stdout, re.MULTILINE)


def test_max_context_beyond_plan_fails(tmp_path):
    result = run_cli(
        tmp_path,
        "serve",
        "--model",
        "mlx-community/Qwen3.8-27B-4bit",
        "--port",
        str(free_port()),
        "--max-context",
        "256K",
        env={"FAKE_SPLASH_AUTO_CONTEXT": "131072", "FAKE_SPLASH_LOAD_SECONDS": "0"},
    )
    assert result.returncode == 1
    assert (
        "error: runtime bootstrap failed [model_creation]: --max-context 262144 exceeds the 131072 tokens the "
        "model and this Mac's memory allow; omit it or pass at most 131072"
    ) in result.stderr


def test_port_in_use(tmp_path):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        result = run_cli(tmp_path, "serve", "--model", "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M", "--port", str(port))
    assert result.returncode == 1
    assert f"error: cannot bind 127.0.0.1:{port}: [Errno 48] Address already in use" in result.stderr


def test_slow_start_not_listening_until_ready(make_engine):
    fake = make_engine(env={"FAKE_SPLASH_START_DELAY": "1.0"}, wait=False)
    fake.wait_for_line("Weights loaded in", 10)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", fake.port), timeout=0.5).close()
    fake.wait_ready(10)
    assert fake.json("GET", "/ready")[0] == 200


def test_listen_early_ready_503_until_ready(make_engine):
    fake = make_engine(env={"FAKE_SPLASH_START_DELAY": "1.0", "FAKE_SPLASH_LISTEN_EARLY": "1"}, wait=False)
    fake.wait_for_line("Weights loaded in", 10)
    deadline = time.monotonic() + 5
    while True:
        try:
            status = fake.json("GET", "/ready")
            break
        except OSError:
            assert time.monotonic() < deadline
            time.sleep(0.05)
    assert status == (503, {"status": "unavailable"})
    fake.wait_ready(10)
    assert fake.json("GET", "/ready")[0] == 200


def test_sigint_clean_stop(make_engine):
    fake = make_engine()
    assert fake.stop() == 0
    fake.wait_for_line(TS + r"Stopping · releasing engine resources$", stream="stdout")


def test_sigterm_also_stops(make_engine):
    fake = make_engine()
    fake.process.send_signal(signal.SIGTERM)
    assert fake.process.wait(10) == 0


def test_persistent_cache_flush_and_take_back(tmp_path, make_engine):
    cache = tmp_path / "cache"
    args = ["--no-webui", "--max-cache-disk", "1G", "--persistent-cache", "--cache-dir", str(cache)]
    first = make_engine(base=tmp_path, args=args, env={"FAKE_SPLASH_FLUSH_SECONDS": "0.3"})
    first.wait_for_line(r"Persistent cache tier: 1024 MiB for KV pages of ", stream="stderr")
    first.wait_for_line(
        r"Persistent cache .*: took back 0 restore points over 0 KV blocks \(0 MiB\); left 0 copies behind\.$"
    )
    for i in range(3):
        assert first.json("POST", "/v1/chat/completions", chat_body(f"turn {i} " + "x " * 80))[0] == 200
    started = time.monotonic()
    assert first.stop() == 0
    assert time.monotonic() - started >= 0.3
    second = make_engine(base=tmp_path, args=args)
    line = second.wait_for_line(r"Persistent cache .*: took back \d+ restore points")
    assert "took back 0 " not in line and "probation" not in line
    taken = second.json("GET", "/status")[1]["disk"]
    assert taken["persistent"] is True and taken["taken_back"]["states"] > 0


def test_second_sigint_skips_flush_and_marks_unclean(tmp_path, make_engine):
    cache = tmp_path / "cache"
    args = ["--no-webui", "--max-cache-disk", "1G", "--persistent-cache", "--cache-dir", str(cache)]
    first = make_engine(base=tmp_path, args=args, env={"FAKE_SPLASH_FLUSH_SECONDS": "10"})
    first.interrupt()
    first.wait_for_line("Stopping · releasing engine resources")
    started = time.monotonic()
    first.process.send_signal(signal.SIGINT)
    assert first.process.wait(5) == 0
    assert time.monotonic() - started < 3
    second = make_engine(base=tmp_path, args=args)
    second.wait_for_line("The last process did not stop cleanly: this one serves on probation for its first minute.")


def test_cache_dir_in_use_by_another_process(tmp_path, make_engine):
    args = ["--no-webui", "--max-cache-disk", "1G", "--persistent-cache", "--cache-dir", str(tmp_path / "cache")]
    make_engine(base=tmp_path / "a", args=args)
    second = make_engine(base=tmp_path / "b", args=args)
    second.wait_for_line(r"is in use by another process; this one keeps a temporary cache\.$")
    second.wait_for_line(r"Cache disk tier: 1024 MiB")
    assert second.json("GET", "/status")[1]["disk"]["persistent"] is False


def test_agent_launcher_reads_models(make_engine, tmp_path):
    fake = make_engine()
    result = run_cli(tmp_path, "claude", "--print", "hi", env={"SPLASH_PORT": str(fake.port)})
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[0] == "Starting claude: unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M · 262,144 context tokens"
    assert lines[1] == "Claude hosted WebSearch is unavailable. WebFetch, local tools and MCP are unchanged."
    launched = json.loads(lines[2])
    assert launched["args"] == ["--print", "hi"] and launched["input_modalities"] == ["text", "image", "pdf"]
    codex = run_cli(tmp_path, "codex", env={"SPLASH_PORT": str(fake.port)})
    assert codex.stdout.splitlines()[1].startswith("Codex hosted WebSearch is disabled: Splash does not provide")


def test_agent_launcher_without_server(tmp_path):
    port = free_port()
    result = run_cli(tmp_path, "opencode", env={"SPLASH_PORT": str(port)})
    assert result.returncode == 1
    assert result.stderr == (
        f"error: No ready Splash server at http://127.0.0.1:{port}. "
        "Run 'splash serve --model <HF_REPO_ID>' in another terminal first.\n"
    )


def test_device_check_refusal_before_install(tmp_path):
    result = run_cli(
        tmp_path,
        "serve",
        "--model",
        "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M",
        "--port",
        str(free_port()),
        env={"FAKE_SPLASH_DEVICE_CHECK": "fail"},
    )
    assert result.returncode == 1
    assert result.stderr.startswith("error: Splash needs Apple GPU family 9 or newer (M3 or later) on macOS 26.4")
    assert (
        "Selected " not in result.stdout
        and not (tmp_path / "models").joinpath("models--unsloth--Qwen3.6-35B-A3B-GGUF").exists()
    )


@pytest.mark.parametrize("args", [["--help"], ["serve", "--help"]])
def test_help_text_matches_real_cli(tmp_path, args):
    fake = run_cli(tmp_path, *args, env={"COLUMNS": "100"})
    assert fake.returncode == 0 and "usage: splash" in fake.stdout
    if not REAL_CLI.exists():
        pytest.skip("no installed Splash to compare with")
    real = subprocess.run(
        [str(REAL_CLI), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "COLUMNS": "100"},
        check=False,
    )
    assert fake.stdout == real.stdout


def test_copied_engine_modules_are_verbatim():
    if not REAL_SPLASH.exists():
        pytest.skip("no ./splash reference clone")
    pkg = Path(SPLASH_BIN).parents[1]
    for name in (
        "serve_options",
        "origins",
        "http_security",
        "errors",
        "images",
        "metrics",
        "latency",
        "json_codec",
        "lru",
        "diagnostics",
    ):
        assert (pkg / "server" / f"{name}.py").read_text() == (REAL_SPLASH / "server" / f"{name}.py").read_text(), name
    assert (pkg / "install/families.py").read_text() == (REAL_SPLASH / "install/families.py").read_text()


def test_fake_data_never_defaults_to_library(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "SPLASH_GUI_FAKE_DATA"}
    pkg = Path(SPLASH_BIN).parents[1]
    python = shutil.which("python3") or "python3"
    out = subprocess.run(
        [
            python,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); from install import paths; print(paths.DATA)",
            str(pkg),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "Library/Application Support" not in out.stdout and "splash-gui-fake-data" in out.stdout

"""Splash 1.5.0 in the fake: full option names only, local model files
(`--model-dir`, `--mmproj`), timings and thinking tokens in Responses and Messages,
reasoning efforts in /v1/models, the "text only" Ready line, the installer's JSON
progress and its cleanup of abandoned partial downloads."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest
from test_installer import install, repo_dir

from harness import PKG, SPLASH_BIN, FakeSplash, fake_env

DENSE = "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M"
MOE = "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"
FLINT = "unsloth/flint-GGUF:UD-Q4_K_XL"


def splash(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SPLASH_BIN), *args],
        env=fake_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_clients_py_is_verbatim():
    upstream = PKG.parents[2] / "splash" / "install" / "clients.py"
    if upstream.is_file():
        assert (PKG / "install" / "clients.py").read_bytes() == upstream.read_bytes()


def test_serve_refuses_abbreviated_option_names(tmp_path):
    result = splash(tmp_path, "serve", "--model", DENSE, "--max-mem", "20G")
    assert result.returncode == 2
    assert "unrecognized arguments: --max-mem 20G" in result.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--mmproj", "/x/mmproj.gguf"], "--mmproj requires --model-dir"),
        (["--model-dir", "/x/m.gguf", "--revision", "main"], "--revision cannot be used with --model-dir"),
        (
            ["--model-dir", "/x/m.gguf", "--draft-model", "incoai/Qwen3.8-27B-DFlash2"],
            "--draft-model must be a local DFlash2 draft directory with --model-dir",
        ),
        (
            ["--model-dir", "/x/m.gguf", "--mmproj", "/x/p.gguf", "--language-only"],
            "--mmproj cannot be used with --language-only",
        ),
    ],
)
def test_local_model_options_are_checked_as_the_launcher_does(tmp_path, args, message):
    result = splash(tmp_path, "serve", "--model", "local/qwen", *args)
    assert result.returncode == 2 and message in result.stderr, result.stderr


def test_serve_help_names_the_local_model_options(tmp_path):
    out = splash(tmp_path, "serve", "--help").stdout
    assert "--model-dir PATH" in out and "--mmproj FILE" in out
    assert "Load an upstream or local model with its DFlash2 draft" in out


def test_a_text_only_start_says_text_only(tmp_path):
    with FakeSplash(tmp_path, model=FLINT) as engine:
        assert engine.wait_for_line(r"Ready · .* · text only · http://")


def test_models_report_reasoning_efforts(tmp_path):
    with FakeSplash(tmp_path, model=DENSE) as engine:
        entry = engine.json("GET", "/v1/models")[1]["data"][0]
        assert entry["reasoning"] == {"thinks": True, "efforts": ["low", "medium", "xhigh"], "default": "xhigh"}
    # The server's default effort, named by the listed effort it renders like.
    args = ("--no-webui", "--default-reasoning-effort", "minimal")
    with FakeSplash(tmp_path / "minimal", model=DENSE, args=args) as engine:
        entry = engine.json("GET", "/v1/models")[1]["data"][0]
        assert entry["reasoning"] == {"thinks": True, "efforts": ["low", "medium", "xhigh"], "default": "low"}
    args = ("--no-webui", "--default-reasoning-effort", "none")
    with FakeSplash(tmp_path / "none", model=MOE, args=args) as engine:
        entry = engine.json("GET", "/v1/models")[1]["data"][0]
        assert entry["reasoning"] == {"thinks": False, "efforts": [], "default": None}
    with FakeSplash(tmp_path / "flint", model=FLINT) as engine:
        entry = engine.json("GET", "/v1/models")[1]["data"][0]
        assert entry["reasoning"] == {"thinks": None, "efforts": [], "default": None}


def test_responses_report_timings_and_no_cache_writes(tmp_path):
    with FakeSplash(tmp_path, model=MOE) as engine:
        body = {"input": "tell me about tides", "max_output_tokens": 40}
        status, response = engine.json("POST", "/v1/responses", body)
        assert status == 200, response
        usage, timings = response["usage"], response["timings"]
        assert usage["input_tokens_details"]["cache_write_tokens"] == 0
        assert timings["prompt_n"] == usage["input_tokens"]
        assert timings["predicted_n"] == usage["output_tokens"]
        assert timings["cache_n"] == usage["input_tokens_details"]["cached_tokens"]
        assert timings["predicted_ms"] >= 0 and timings["prompt_ms"] >= 0


def test_messages_stream_ends_with_timings_and_thinking_tokens(tmp_path):
    with FakeSplash(tmp_path, model=MOE) as engine:
        body = {
            "model": MOE,
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": "hello"}],
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "stream": True,
        }
        status, _, payload = engine.request("POST", "/v1/messages", body)
        assert status == 200, payload
        events = [json.loads(line[5:]) for line in payload.decode().splitlines() if line.startswith("data:")]
        delta = next(e for e in events if e.get("type") == "message_delta")
        assert delta["usage"]["output_tokens_details"]["thinking_tokens"] > 0
        assert delta["timings"]["predicted_n"] == delta["usage"]["output_tokens"]
        assert "timings" not in events[0]["message"]


def test_prepare_reports_json_progress(tmp_path):
    env = {"FAKE_SPLASH_DL_BPS": "8M", "FAKE_SPLASH_DL_SHARD_BYTES": "4M"}
    code, out, err = install(tmp_path, "--model", DENSE, "prepare", "--json-progress", env=env)
    assert code == 0, err
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    target = [e for e in events if e["repo"] == "unsloth/Qwen3.8-27B-GGUF"]
    assert target[0]["done"] == 0 and target[-1]["done"] == target[-1]["total"] > 0
    assert all(e["event"] == "download" for e in events)
    assert [e["done"] for e in target] == sorted(e["done"] for e in target)
    # Without the flag, nothing but the installer's own lines.
    code, out, _ = install(tmp_path / "plain", "--model", DENSE, "prepare")
    assert code == 0 and not any(line.startswith("{") for line in out.splitlines())


def test_a_download_removes_abandoned_partials_but_not_locked_ones(tmp_path):
    """install/hub.py _remove_abandoned_downloads: every `<blob>.*.incomplete` whose
    lock is free goes, a locked one stays, and a name without `.incomplete` (the
    manager's `<etag>.splashgui.partial`) is never touched."""
    folder = repo_dir(tmp_path, "unsloth/Qwen3.8-27B-GGUF")
    blobs = folder / "blobs"
    blobs.mkdir(parents=True)
    abandoned = blobs / ("a" * 64 + ".0123abcd.incomplete")
    old_manager = blobs / ("b" * 64 + ".splashgui.incomplete")
    locked = blobs / ("c" * 64 + ".89abcdef.incomplete")
    manager = blobs / ("d" * 64 + ".splashgui.partial")
    for path in (abandoned, old_manager, locked, manager):
        path.write_bytes(b"x" * 10)
    locks = folder.parent / ".locks" / folder.name
    locks.mkdir(parents=True)
    with (locks / ("c" * 64 + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        code, _, err = install(tmp_path, "--model", DENSE, "prepare")
    assert code == 0, err
    assert not abandoned.exists() and not old_manager.exists()
    assert locked.exists() and manager.exists()
    assert os.environ.get("FAKE_SPLASH_DL_HUB") is None

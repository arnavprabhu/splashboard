"""Benchmarks against the fake engine (SPEC §10.6, §15.3 `benchmark_runs`)."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from splash_gui.app import AppConfig, create_app
from splash_gui.benchmark.service import headline, running_gpu_apps, summarize
from splash_gui.paths import Paths
from splash_gui.secrets import MemoryBackend, SecretStore
from splash_gui.usage.db import UsageDB, iso

from .conftest import fake_engine
from .fakeengine import MODEL, EngineHarness

H = Callable[..., EngineHarness]
FAST = {"FAKE_SPLASH_TOKS": "20000", "FAKE_SPLASH_PREFILL_TPS": "10000000"}
SMALL = {
    "scenarios": ["decode_short", "cold_prefill", "cached_ttft", "concurrency"],
    "samples": 1,
    "prefill_tokens": [2048],
    "concurrency": [1, 2],
}


def wait_run(h: EngineHarness, run_id: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        run = h.client.get(f"/api/admin/benchmark/runs/{run_id}").json()
        if run["state"] != "running":
            return dict(run)
        assert time.monotonic() < deadline, run
        time.sleep(0.05)


@pytest.fixture
def h(harness_factory: H) -> EngineHarness:
    harness = harness_factory(env=FAST)
    assert harness.load()["state"] == "ready"
    return harness


def test_a_full_run_is_saved_with_metrics(h: EngineHarness) -> None:
    started = h.client.post("/api/admin/benchmark", json=SMALL)
    assert started.status_code == 202, started.text
    run = wait_run(h, started.json()["run_id"])
    assert run["state"] == "done", run
    assert run["model"] == MODEL and run["engine_version"] == "1.3.0"
    assert run["hardware"] and "power" in run and run["settings"]["version"] == 2
    scenarios = [(r["scenario"], r["params"]["value"]) for r in run["results"]]
    assert scenarios == [
        ("decode_short", 128),
        ("cold_prefill", 2048),
        ("cached_ttft", 32768),
        ("concurrency", 1),
        ("concurrency", 2),
    ]
    by = {(r["scenario"], r["params"]["value"]): r for r in run["results"]}
    decode = by[("decode_short", 128)]
    assert decode["params"]["max_tokens"] == 512 and decode["params"]["prompt_tokens"] == 128
    assert decode["summary"]["decode_tps"] and decode["summary"]["ttft_p50_ms"] is not None
    assert decode["summary"]["status_delta"]["decode_output_tokens"] >= 512
    cold = by[("cold_prefill", 2048)]
    assert cold["params"]["max_tokens"] == 1 and cold["summary"]["prefill_tps"]
    cached = by[("cached_ttft", 32768)]
    assert cached["summary"]["cached_tokens"], "the replayed prompt hits the cache"
    pair = by[("concurrency", 2)]
    assert len(pair["samples"][0]["requests"]) == 2 and pair["summary"]["aggregate_tps"]
    listing = h.client.get("/api/admin/benchmark/runs").json()["runs"]
    assert listing[0]["id"] == run["id"]
    line = listing[0]["headline"]
    assert line["decode_tps"] and line["prefill_tps_2048"] and line["concurrency_peak_tps"]
    assert line["cached_ttft_ms"] is not None
    # Benchmark traffic does not pollute the usage history.
    assert h.client.get("/api/admin/usage/summary").json()["requests"] == 0
    assert h.sup.in_flight == 0, "every benchmark request was counted out again"


def test_benchmark_requests_run_at_foreground_priority(h: EngineHarness) -> None:
    run_id = h.client.post(
        "/api/admin/benchmark", json={**SMALL, "scenarios": ["decode_short"]}
    ).json()["run_id"]
    assert wait_run(h, run_id)["state"] == "done"
    bodies = [
        r["body"]
        for r in h.last_engine_requests()
        if r["path"] == "/v1/completions" and r["body"] is not None
    ]
    assert bodies and all(b["priority"] == "foreground" for b in bodies)
    assert all(b["ignore_eos"] is True and b["temperature"] == 0 for b in bodies)


def test_cancel_stops_the_run(harness_factory: H) -> None:
    h = harness_factory(env={"FAKE_SPLASH_TOKS": "200", "FAKE_SPLASH_PREFILL_TPS": "10000000"})
    h.load()
    run_id = h.client.post(
        "/api/admin/benchmark", json={**SMALL, "scenarios": ["decode_short"], "samples": 5}
    ).json()["run_id"]
    deadline = time.monotonic() + 10
    while h.engine()["requests_in_flight"] == 0:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert h.client.post("/api/admin/benchmark/cancel").json()["cancelled"] is True
    run = h.client.get(f"/api/admin/benchmark/runs/{run_id}").json()
    assert run["state"] == "cancelled"
    assert h.client.post("/api/admin/benchmark/cancel").json()["cancelled"] is False


def test_one_run_at_a_time_and_parameter_checks(harness_factory: H) -> None:
    h = harness_factory(env={"FAKE_SPLASH_TOKS": "200"})
    h.load()
    bad = h.client.post("/api/admin/benchmark", json={"scenarios": []})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_benchmark"
    assert h.client.post("/api/admin/benchmark", json={"concurrency": [8]}).status_code == 400
    assert h.client.post("/api/admin/benchmark", json={"samples": 0}).status_code == 422
    first = h.client.post("/api/admin/benchmark", json={**SMALL, "scenarios": ["decode_short"]})
    assert first.status_code == 202
    second = h.client.post("/api/admin/benchmark", json=SMALL)
    assert second.status_code == 409 and second.json()["error"]["code"] == "benchmark_running"
    running = h.client.delete(f"/api/admin/benchmark/runs/{first.json()['run_id']}")
    assert running.status_code == 409
    h.client.post("/api/admin/benchmark/cancel")


def test_benchmark_needs_a_loaded_model(harness_factory: H) -> None:
    h = harness_factory()
    response = h.client.post("/api/admin/benchmark", json=SMALL)
    assert response.status_code == 503 and response.json()["error"]["code"] == "engine_unavailable"
    preflight = h.client.post("/api/admin/benchmark/preflight").json()
    assert preflight["ready"] is False and "No model is loaded." in preflight["warnings"]


def test_delete_and_missing_runs(h: EngineHarness) -> None:
    run_id = h.client.post(
        "/api/admin/benchmark", json={**SMALL, "scenarios": ["decode_short"]}
    ).json()["run_id"]
    wait_run(h, run_id)
    assert h.client.delete(f"/api/admin/benchmark/runs/{run_id}").status_code == 204
    assert h.client.get(f"/api/admin/benchmark/runs/{run_id}").status_code == 404
    assert h.client.delete(f"/api/admin/benchmark/runs/{run_id}").status_code == 404


def test_an_engine_crash_mid_run_fails_it(harness_factory: H) -> None:
    h = harness_factory(env={"FAKE_SPLASH_TOKS": "100", "FAKE_SPLASH_PREFILL_TPS": "10000000"})
    h.patch_settings({"global": {"lifecycle": {"auto_restart": False}}})
    h.load()
    run_id = h.client.post(
        "/api/admin/benchmark", json={**SMALL, "scenarios": ["decode_short"], "samples": 3}
    ).json()["run_id"]
    deadline = time.monotonic() + 10
    while h.engine()["requests_in_flight"] == 0:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    h.fake("POST", "/_fake/crash", {"signal": "SIGKILL"})
    run = wait_run(h, run_id, timeout=30)
    assert run["state"] == "failed" and run["error"]


def test_an_interrupted_run_is_marked_failed_at_startup(
    isolated_home: Path, tmp_path: Path
) -> None:
    """A manager crash mid-run leaves a `running` row; the next start closes it."""
    paths = Paths(isolated_home).ensure()
    db = UsageDB(paths.usage_db)
    db.save_benchmark(
        {
            "id": "r1",
            "ts": iso(),
            "model": MODEL,
            "state": "running",
            "results": [
                {"scenario": "decode_short", "params": {"value": 128}, "samples": [], "summary": {}}
            ],
        }
    )
    db.save_benchmark({"id": "r2", "ts": iso(), "model": MODEL, "state": "done", "results": []})
    db.close()
    app = create_app(
        AppConfig(paths=paths, web_dist=tmp_path / "dist", secrets=SecretStore(MemoryBackend()))
    )
    app.state.manager.discover_engine = fake_engine
    with TestClient(app, client=("127.0.0.1", 5)) as client:
        token = app.state.manager.auth.cli_token()
        headers = {"Authorization": f"Bearer {token}"}
        run = client.get("/api/admin/benchmark/runs/r1", headers=headers).json()
        assert run["state"] == "failed"
        assert run["error"] == "Manager stopped during this run"
        assert len(run["results"]) == 1, "partial results are kept"
        assert client.get("/api/admin/benchmark/runs/r2", headers=headers).json()["state"] == "done"
        assert client.delete("/api/admin/benchmark/runs/r1", headers=headers).status_code == 204


def test_running_gpu_apps() -> None:
    assert running_gpu_apps(["launchd", "Blender", "UnrealEditor"], []) == [
        "Blender",
        "Unreal Editor",
    ]
    assert running_gpu_apps(["zsh"], ["-zsh"]) == []


def test_gpu_apps_ignore_helpers_and_idle_model_servers() -> None:
    """QA row 20: "Unreal" matched `UnrealEditorServices`, a background helper, and an
    idle `ollama serve` counted as GPU-heavy."""
    names = ["UnrealEditorServices", "UnityHub", "Ollama", "ollama", "MotionCore"]
    idle = ["/Applications/Ollama.app/Contents/Resources/ollama serve"]
    assert running_gpu_apps(names, idle) == []
    loaded = [
        *idle,
        "/Applications/Ollama.app/Contents/Resources/ollama runner --model /x/blobs/sha256-1",
    ]
    assert running_gpu_apps(names, loaded) == ["Ollama (model loaded)"]
    assert running_gpu_apps([], ["/usr/local/bin/ollama_llama_server --model x"]) == [
        "Ollama (model loaded)"
    ]
    assert running_gpu_apps(["Python"], ["python3 -m mlx_lm.server --model m"]) == ["mlx_lm"]
    assert running_gpu_apps([], ["/opt/bin/mlx_lm.server --model m"]) == ["mlx_lm"]
    console_script = "/venv/bin/python3.12 /venv/bin/mlx_lm.server --model m --port 8080"
    assert running_gpu_apps(["Python"], [console_script]) == ["mlx_lm"]
    assert running_gpu_apps(["Python"], ["/venv/bin/python3.12 /venv/bin/other.py"]) == []
    # A shell whose command line only mentions them is not one of them.
    assert running_gpu_apps([], ["zsh -c echo ollama runner; python -m mlx_lm"]) == []


def test_summaries_and_headline() -> None:
    samples = [
        {
            "aggregate_tps": 100.0,
            "requests": [
                {
                    "ttft_ms": 10.0,
                    "prompt_tokens": 2000,
                    "prompt_ms": 100.0,
                    "completion_tokens": 50,
                    "predicted_ms": 500.0,
                    "cached_tokens": 0,
                }
            ],
        }
    ]
    delta = {
        "decode_output_tokens": 50,
        "decode_cycle_ms": 250.0,
        "drafted_tokens": 40,
        "accepted_draft_tokens": 30,
    }
    summary = summarize("decode_short", samples, delta)
    assert summary["decode_tps"] == 100.0 and summary["prefill_tps"] == 20000.0
    assert summary["status_decode_tps"] == 200.0 and summary["draft_acceptance"] == 0.75
    line = headline(
        [
            {"scenario": "decode_short", "params": {"value": 128}, "summary": summary},
            {"scenario": "concurrency", "params": {"value": 1}, "summary": {"aggregate_tps": 5.0}},
            {"scenario": "concurrency", "params": {"value": 2}, "summary": {"aggregate_tps": 9.0}},
        ]
    )
    assert line == {"decode_tps": 100.0, "concurrency_peak_tps": 9.0}

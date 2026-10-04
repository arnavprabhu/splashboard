"""Engine supervision against the fake engine (SPEC §6.3–§6.6)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from splash_gui.engine.discovery import EngineInfo

from .conftest import write_script
from .fakeengine import MODEL, MODEL_27B, EngineHarness


def test_load_reaches_ready_and_logs_session(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory()
    view = h.load()
    assert view["state"] == "ready", view
    assert view["model"] == MODEL
    assert view["maximum_context_tokens"] == 262144
    assert view["chat_template_mode"] == "patched"
    assert view["vision"] is True
    assert view["draft"] is None or isinstance(view["draft"], str)
    assert view["command"] and "--no-webui" in view["command"]
    assert "SPLASH_API_KEY=••••••" in view["command"]
    assert view["pid"] and view["internal_port"]
    assert view["restart"]["auto_restart"] is True
    log = (h.home / "logs" / "engine.log").read_text()
    assert "engine session started" in log
    assert "Ready · " in log
    assert "splash-internal-" not in log
    h.client.post("/api/admin/engine/stop")
    view = h.wait_state("stopped")
    assert view["pid"] is None
    log = (h.home / "logs" / "engine.log").read_text()
    assert "engine session ended · stop" in log
    sessions = h.state.usage.sessions()
    assert sessions[0]["model"] == MODEL and sessions[0]["reason"] == "stop"
    assert not (h.home / "run" / "engine.pid").exists()


def test_engine_environment_and_flags(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory()
    h.load()
    fake = h.fake("GET", "/_fake/state")
    env = fake["env"]
    assert env["HF_HUB_CACHE"] == str(h.home / "models")
    assert env["TMPDIR"] == str(h.home / "cache" / "tmp")
    assert fake["api_key_set"] is True
    argv = fake["argv"]
    assert (
        "--no-webui" in argv and "--host" in argv and argv[argv.index("--host") + 1] == "127.0.0.1"
    )


def test_load_validation_errors(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory()
    bad = h.client.post("/api/admin/engine/load", json={"model": "not a model"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_model_id"
    missing = h.client.post("/api/admin/engine/load", json={"model": "a/b"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "model_not_installed"
    assert MODEL in missing.json()["error"]["details"]["installed"]
    restart = h.client.post("/api/admin/engine/restart")
    assert restart.status_code == 409


def test_profile_id_loads_the_base_model(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory()
    response = h.client.post(
        "/api/admin/engine/load", json={"model": f"{MODEL}:no-think", "wait": True}
    )
    assert response.status_code == 202
    assert response.json()["state"] == "ready" and response.json()["model"] == MODEL


def test_crash_restarts_with_backoff_then_fails(
    harness_factory: Callable[..., EngineHarness],
) -> None:
    h = harness_factory()
    h.load()
    for attempt in (1, 2):
        h.fake("POST", "/_fake/crash", {"code": 3})
        view = h.wait_state("crashed", "starting", "ready", timeout=10)
        view = h.wait_state("ready", timeout=20)
        assert view["restart"]["crashes_in_window"] == attempt
    events = h.state.usage.sessions()
    assert any(s["reason"] == "crash" for s in events)
    h.fake("POST", "/_fake/crash", {"signal": "SIGKILL"})
    view = h.wait_state("failed", timeout=10)
    assert view["error"]["kind"] == "crash_loop"
    alerts = h.client.get("/api/admin/alerts").json()["alerts"]
    assert any(a["condition"] == "crash_loop" and a["severity"] == "critical" for a in alerts)
    # Restart from failed works and clears the history.
    h.client.post("/api/admin/engine/restart")
    assert h.wait_state("ready", timeout=20)["restart"]["crashes_in_window"] == 0


def test_crash_without_auto_restart_fails(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory()
    settings = h.client.get("/api/admin/settings").json()["settings"]
    settings["global"]["lifecycle"]["auto_restart"] = False
    assert h.client.put("/api/admin/settings", json=settings).status_code == 200
    h.load()
    h.fake("POST", "/_fake/crash", {"code": 1})
    view = h.wait_state("failed", timeout=10)
    assert view["error"]["code"] == "crashed"


def test_budget_refusal_fails_with_breakdown(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory(env={"FAKE_SPLASH_FAIL_STARTUP": "budget"})
    view = h.load()
    assert view["state"] == "failed"
    error = view["error"]
    assert error["kind"] == "budget_refusal"
    assert error["budget"] and error["budget"][0]["label"] == "physical memory"
    assert {s["action"] for s in error["suggestions"]} >= {"lower_max_context", "language_only"}
    assert view["log_tail"]
    # Stop from failed returns to stopped and clears the error.
    assert h.client.post("/api/admin/engine/stop").json()["state"] == "stopped"


def test_switch_model_and_busy_conflict(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory(installed=(MODEL, MODEL_27B), env={"FAKE_SPLASH_TOKS": "40"})
    h.load(MODEL)
    done = threading.Event()

    def slow() -> None:
        h.client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 80,
                "ignore_eos": True,
                "stream": True,
            },
        )
        done.set()

    worker = threading.Thread(target=slow)
    worker.start()
    deadline = time.monotonic() + 5
    while h.engine()["requests_in_flight"] == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert h.engine()["state"] == "busy"
    busy = h.client.post("/api/admin/engine/load", json={"model": MODEL_27B})
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "model_switch_busy"
    worker.join(30)
    assert done.is_set()
    h.wait_state("ready")
    view = h.load(MODEL_27B)
    assert view["state"] == "ready" and view["model"] == MODEL_27B
    reasons = [s["reason"] for s in h.state.usage.sessions()]
    assert "switch" in reasons


def test_recovering_and_engine_failed(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory()
    h.load()
    h.fake("POST", "/_fake/mode", {"mode": "engine_recovering"})
    view = h.wait_state("recovering", timeout=10)
    assert view["transport"]["recovering"] is True
    h.fake("POST", "/_fake/mode", {"mode": "normal"})
    h.wait_state("ready", timeout=10)
    h.fake("POST", "/_fake/mode", {"mode": "engine_failed"})
    view = h.wait_state("engine_failed", timeout=10)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        alerts = h.client.get("/api/admin/alerts").json()["alerts"]
        if any(a["condition"] == "engine_failed" for a in alerts):
            break
        time.sleep(0.1)
    assert any(a["condition"] == "engine_failed" for a in alerts)
    restart = next(a for a in alerts if a["condition"] == "engine_failed")["actions"]
    assert restart[0]["path"] == "/api/admin/engine/restart"


def test_idle_released_and_restored(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory(env={"FAKE_SPLASH_IDLE_RELEASE_SECONDS": "0.3"})
    h.load()
    view = h.wait_state("idle_released", timeout=10)
    assert view["model"] == MODEL
    response = h.client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 2},
    )
    assert response.status_code == 200
    view = h.wait_state("ready", "idle_released", timeout=10)
    assert any(n["kind"] == "weights_restored" for n in h.engine()["notices"])


def test_idle_unload_stops_the_process(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory()
    settings = h.client.get("/api/admin/settings").json()["settings"]
    settings["global"]["lifecycle"]["idle_unload"] = True
    settings["global"]["lifecycle"]["idle_unload_minutes"] = 5
    assert h.client.put("/api/admin/settings", json=settings).status_code == 200
    h.load()
    h.sup.last_request_mono = time.monotonic() - 299
    h.client.portal.call(h.sup.check_idle)  # type: ignore[union-attr]
    assert h.engine()["state"] == "ready"
    h.sup.last_request_mono = time.monotonic() - 301
    h.client.portal.call(h.sup.check_idle)  # type: ignore[union-attr]
    assert h.wait_state("stopped")["state"] == "stopped"
    assert h.state.usage.sessions()[0]["reason"] == "idle_unload"


def test_auto_load_after_idle_unload(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory()
    response = h.client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 2},
    )
    assert response.status_code == 200, response.text
    assert h.engine()["state"] in ("ready", "busy")


def test_stop_sequence_escalates_to_sigkill(
    harness_factory: Callable[..., EngineHarness], tmp_path: Path
) -> None:
    stubborn = write_script(
        tmp_path / "stubborn" / "splash",
        'trap "" INT\necho "12:00:00 Loading · x"\nexec sleep 60\n',
    )
    h = harness_factory()
    h.state.discover_engine = lambda: EngineInfo(
        found=True,
        cli=stubborn,
        source="setting",
        version="1.2.0",
        version_tuple=(1, 2, 0),
        support="supported",
    )
    h.state.forget_engine()
    h.sup.stop_timeout_s = 0.3
    h.sup.second_sigint_s = 0.3
    response = h.client.post("/api/admin/engine/load", json={"model": MODEL})
    assert response.status_code == 202
    h.wait_state("starting")
    # Wait until the child installed its SIGINT trap, rather than racing process startup.
    deadline = time.monotonic() + 5
    while "Loading · x" not in (h.home / "logs" / "engine.log").read_text():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    started = time.monotonic()
    view = h.client.post("/api/admin/engine/stop").json()
    assert view["state"] == "stopped"
    assert time.monotonic() - started < 5
    log = (h.home / "logs" / "engine.log").read_text()
    assert "engine session ended · stop · exit -9" in log


def test_startup_failure_is_failed_not_a_restart_loop(
    harness_factory: Callable[..., EngineHarness], tmp_path: Path
) -> None:
    broken = write_script(
        tmp_path / "broken" / "splash",
        'echo "error: cannot install a/b: no supported model has this architecture '
        '(hidden_size=4096); supported: Qwen3.8-27B" >&2\nexit 1\n',
    )
    h = harness_factory()
    h.state.discover_engine = lambda: EngineInfo(
        found=True,
        cli=broken,
        source="setting",
        version="1.2.0",
        version_tuple=(1, 2, 0),
        support="supported",
    )
    h.state.forget_engine()
    view = h.load()
    assert view["state"] == "failed"
    assert view["error"]["kind"] == "incompatible"
    assert view["restart"]["crashes_in_window"] == 0


def test_engine_missing(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory()
    h.state.discover_engine = lambda: EngineInfo(found=False, error="Splash is not installed")
    h.state.forget_engine()
    response = h.client.post("/api/admin/engine/load", json={"model": MODEL})
    assert response.status_code == 503 and response.json()["error"]["code"] == "engine_not_found"


@pytest.mark.parametrize("watchers", [0, 1])
def test_status_poll_interval_follows_watchers(
    harness_factory: Callable[..., EngineHarness], watchers: int
) -> None:
    h = harness_factory()
    h.state.watchers = watchers
    assert h.sup.watching() is bool(watchers)

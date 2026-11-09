"""Splash 1.5.0 in the manager: the "text only" Ready line, resumable partials that the
installer's new cleanup of abandoned downloads cannot remove, the installer's own JSON
progress, timings and thinking tokens from Responses and Messages in Usage, the served
model's reasoning efforts, full option names only, and the Splash desktop app check."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path
from typing import Any

import pytest

from splash_gui import hubcache
from splash_gui.downloads.service import installer_progress
from splash_gui.engine import desktop_app
from splash_gui.engine.startup import ReadyEvent, StartupParser
from splash_gui.proxy.shapes import UsageCapture
from splash_gui.settings.validation import managed_flag_for

from .fakeengine import MODEL, MODEL_27B
from .test_downloads import hub_harness  # noqa: F401  (fixture)
from .test_proxy import usage_row

FLINT = "unsloth/flint-GGUF:UD-Q4_K_XL"
SHA = "a" * 64


# --- startup ------------------------------------------------------------------------------


def test_the_ready_line_says_text_only() -> None:
    parser = StartupParser()
    line = f"10:00:00 Ready · {FLINT} · context 256K · text only · http://127.0.0.1:18001"
    (ready,) = [e for e in parser.feed(line) if isinstance(e, ReadyEvent)]
    assert ready.language_only and ready.context_tokens == 262144 and ready.model == FLINT


# --- resumable partials -----------------------------------------------------------------------


def test_the_range_partial_has_no_incomplete_suffix(tmp_path: Path) -> None:
    """Splash 1.5.0 removes `<blob>.*.incomplete` it can lock before it downloads; a
    paused Range partial holds no lock, so its name must not end in `.incomplete`."""
    blob = tmp_path / SHA
    assert hubcache.range_partial(blob).name == f"{SHA}.splashgui.partial"
    assert not hubcache.range_partial(blob).name.endswith(".incomplete")
    assert hubcache.partial_digest(f"{SHA}.splashgui.partial") == SHA
    assert hubcache.partial_digest(f"{SHA}.splashgui.incomplete") == SHA


def test_an_older_managers_partial_is_adopted_with_its_progress(tmp_path: Path) -> None:
    blobs = tmp_path / "models--o--r" / "blobs"
    blobs.mkdir(parents=True)
    legacy = blobs / f"{SHA}.splashgui.incomplete"
    legacy.write_bytes(b"x" * 10)
    (blobs / f"{SHA}.splashgui.json").write_text("{}")
    other = "b" * 64
    taken = blobs / f"{other}.splashgui.incomplete"
    taken.write_bytes(b"old")
    (blobs / f"{other}.splashgui.partial").write_bytes(b"new")
    assert hubcache.adopt_legacy_range_partials(tmp_path) == 1
    assert not legacy.exists() and (blobs / f"{SHA}.splashgui.partial").read_bytes() == b"x" * 10
    # A current partial wins: the old one is left for the stale cleanup.
    assert taken.exists() and (blobs / f"{other}.splashgui.partial").read_bytes() == b"new"
    assert [p.name for p in hubcache.blob_partials(blobs / SHA)] == [f"{SHA}.splashgui.partial"]
    # Removing a legacy partial takes its sidecar too.
    hubcache.remove_partial(taken)
    assert not taken.exists()


async def test_the_downloader_adopts_partials_and_their_record_at_start(harness_factory) -> None:
    from splash_gui.downloads.service import Downloads

    harness = harness_factory(installed=())
    blobs = hubcache.blobs_dir(harness.state.settings.models_dir(), "o/r")
    blobs.mkdir(parents=True)
    (blobs / f"{SHA}.splashgui.incomplete").write_bytes(b"x")
    legacy = str(blobs / f"{SHA}.splashgui.incomplete")
    harness.state.paths.downloads_file.write_text(
        json.dumps({"items": [], "blobs": {}, "preexisting": {"dl1": [legacy, "/x/y.incomplete"]}})
    )
    revived = Downloads(harness.state)
    revived.stopping = True
    await revived.start()
    assert (blobs / f"{SHA}.splashgui.partial").exists()
    adopted = str(blobs / f"{SHA}.splashgui.partial")
    assert revived.preexisting["dl1"] == [adopted, "/x/y.incomplete"]


def test_a_paused_partial_survives_another_download_from_its_repository(
    hub_harness,  # noqa: F811  (fixture)
) -> None:
    """End to end against the fake installer, which removes abandoned partials as 1.5.0
    does: a Range partial and its sidecar stay, an abandoned per-process one goes."""
    from .test_downloads import queue, wait_for

    harness = hub_harness()
    blobs = hubcache.blobs_dir(harness.state.settings.models_dir(), "unsloth/Qwen3.6-35B-A3B-GGUF")
    blobs.mkdir(parents=True)
    paused = blobs / f"{SHA}.splashgui.partial"
    paused.write_bytes(b"x" * 100)
    (blobs / f"{SHA}.splashgui.json").write_text("{}")
    abandoned = blobs / ("c" * 64 + ".0123abcd.incomplete")
    abandoned.write_bytes(b"y")
    started = queue(harness, MODEL)
    assert wait_for(harness, started["id"], "done", "failed")["state"] == "done"
    assert paused.read_bytes() == b"x" * 100 and (blobs / f"{SHA}.splashgui.json").exists()
    assert not abandoned.exists()


# --- installer progress -----------------------------------------------------------------------


def test_installer_progress_lines() -> None:
    line = json.dumps({"event": "download", "repo": "o/r", "done": 5, "total": 10})
    assert installer_progress(line) == ("o/r", 5, 10)
    over = json.dumps({"event": "download", "repo": "o/r", "done": 12, "total": 10})
    assert installer_progress(over) == ("o/r", 10, 10)
    for other in (
        "Fetching 2 file(s), 0.01 GB, from o/r@abc; cached files are reused.",
        '{"event": "other"}',
        '{"event": "download", "repo": "o/r", "done": true, "total": 10}',
        '{"event": "download", "repo": "o/r", "done": -1, "total": 10}',
        "{not json",
    ):
        assert installer_progress(other) is None, other


def test_prepare_asks_for_json_progress_and_verify_does_not(harness_factory) -> None:
    harness = harness_factory(installed=())
    argv, _ = harness.state.models.installer(MODEL, "prepare", json_progress=True)
    assert argv[-2:] == ["prepare", "--json-progress"]
    argv, _ = harness.state.models.installer(MODEL, "verify", json_progress=True)
    assert argv[-1] == "verify"


def test_a_download_counts_the_installers_progress_and_keeps_it_out_of_the_log(
    hub_harness,  # noqa: F811  (fixture)
) -> None:
    from .test_downloads import queue, wait_for

    harness = hub_harness({"FAKE_SPLASH_DL_BPS": "4M", "FAKE_SPLASH_DL_SHARD_BYTES": "2M"})
    started = queue(harness, MODEL)
    done = wait_for(harness, started["id"], "done")
    assert done["bytes_done"] == done["bytes_total"]
    assert not any(line.startswith('{"event"') for line in done["log_tail"]), done["log_tail"]


def test_the_installers_count_lifts_a_file_whose_partial_lags(harness_factory) -> None:
    """Under Xet a partial can stay at 0 while the bytes arrive; the installer's count wins."""
    from splash_gui.schemas import DownloadFile, DownloadItem

    harness = harness_factory(installed=())
    downloads = harness.state.downloads
    item = DownloadItem.model_validate(
        {
            "id": "dl1",
            "model": MODEL,
            "kind": "model",
            "state": "running",
            "created_at": "2026-10-29T00:00:00Z",
            "files": [
                DownloadFile(name="a.gguf", repo_id="o/r", size_bytes=1000).model_dump(),
                DownloadFile(name="config.json", repo_id="o/r", size_bytes=10).model_dump(),
            ],
            "bytes_total": 1010,
        }
    )
    downloads.reported["dl1"] = {"o/r": (400, 1000)}  # config.json was already there
    downloads.progress(item)
    # 10 bytes were there before the installer's fetch, 400 of its 1000 arrived.
    assert item.bytes_done == 410
    assert item.progress == pytest.approx(410 / 1010)


# --- usage ---------------------------------------------------------------------------------


def capture(shape: Any, payloads: list[dict[str, Any]], *, stream: bool) -> UsageCapture:
    usage = UsageCapture(shape, "/v1/x", stream=stream)
    if stream:
        for payload in payloads:
            event = payload.get("type", "")
            usage.feed(f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode())
    else:
        usage.feed(json.dumps(payloads[0]).encode())
    usage.finish()
    return usage


TIMINGS = {
    "prompt_n": 120,
    "prompt_ms": 80.0,
    "predicted_n": 30,
    "predicted_ms": 600.0,
    "cache_n": 100,
}


def test_responses_timings_are_captured() -> None:
    response = {
        "object": "response",
        "status": "completed",
        "output": [{"type": "message"}],
        "usage": {
            "input_tokens": 120,
            "input_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 0},
            "output_tokens": 30,
        },
        "timings": TIMINGS,
    }
    for stream, payloads in (
        (False, [response]),
        (True, [{"type": "response.completed", "response": response}]),
    ):
        usage = capture("responses", payloads, stream=stream)
        assert (usage.prompt_ms, usage.predicted_ms) == (80.0, 600.0)
        assert (usage.prompt_tokens, usage.cached_tokens, usage.completion_tokens) == (120, 100, 30)


def test_messages_count_the_whole_prompt_and_read_timings() -> None:
    start = {
        "type": "message_start",
        "message": {
            "usage": {"input_tokens": 20, "cache_read_input_tokens": 100, "output_tokens": 0}
        },
    }
    delta = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 30, "output_tokens_details": {"thinking_tokens": 12}},
        "timings": TIMINGS,
    }
    usage = capture("messages", [start, delta], stream=True)
    assert (usage.prompt_tokens, usage.cached_tokens, usage.completion_tokens) == (120, 100, 30)
    assert (usage.prompt_ms, usage.predicted_ms) == (80.0, 600.0)
    message = {
        "type": "message",
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 20, "cache_read_input_tokens": 100, "output_tokens": 30},
        "timings": TIMINGS,
    }
    usage = capture("messages", [message], stream=False)
    assert (usage.prompt_tokens, usage.predicted_ms) == (120, 600.0)


def test_agent_traffic_records_decode_timings(harness_factory) -> None:
    harness = harness_factory()
    assert harness.load()["state"] == "ready"
    response = harness.client.post(
        "/v1/messages",
        json={"model": MODEL, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200, response.text
    row = usage_row(harness, response.headers["x-splash-request-id"])
    assert row["predicted_ms"] is not None and row["prompt_ms"] is not None
    assert row["prompt_tokens"] == response.json()["timings"]["prompt_n"]
    response = harness.client.post(
        "/v1/responses", json={"model": MODEL, "input": "hi", "max_output_tokens": 32}
    )
    assert response.status_code == 200, response.text
    row = usage_row(harness, response.headers["x-splash-request-id"])
    assert row["predicted_ms"] is not None and row["completion_tokens"] > 0


# --- reasoning ---------------------------------------------------------------------------------


def test_the_engine_view_carries_the_served_models_reasoning(harness_factory) -> None:
    harness = harness_factory(installed=(MODEL_27B, FLINT))
    view = harness.load(MODEL_27B)
    efforts = ["low", "medium", "xhigh"]
    assert view["reasoning"] == {"thinks": True, "efforts": efforts, "default": "xhigh"}
    listing = harness.client.get("/v1/models").json()["data"]
    assert next(m for m in listing if m["id"] == MODEL_27B)["reasoning"]["efforts"] == [
        "low",
        "medium",
        "xhigh",
    ]
    view = harness.load(FLINT)
    assert view["reasoning"] == {"thinks": None, "efforts": [], "default": None}
    harness.client.post("/api/admin/engine/stop")
    assert harness.wait_state("stopped")["reasoning"] is None


# --- options ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "managed"),
    [("--model-dir", "--model-dir"), ("--mmproj", "--mmproj"), ("--max-mem", "--max-memory")],
)
def test_local_model_flags_and_abbreviations_are_not_extra_flags(flag: str, managed: str) -> None:
    assert managed_flag_for(flag) == managed


def test_an_abbreviated_extra_flag_is_refused_with_the_full_name(harness_factory) -> None:
    harness = harness_factory(installed=())
    document = harness.settings_document()
    extra = [{"flag": "--max-mem", "value": "8G"}]
    document["global"].setdefault("engine", {})["extra_flags"] = extra
    response = harness.client.put("/api/admin/settings", json=document)
    assert response.status_code == 422, response.text
    assert "Splash takes full option names only" in response.text


# --- the Splash desktop app ----------------------------------------------------------------


def make_app(root: Path, version: str | None, *, bundle_id: str = desktop_app.BUNDLE_ID) -> Path:
    app = root / "Splash.app"
    (app / "Contents" / "Resources" / "splash").mkdir(parents=True)
    info = {"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": "9.9"}
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    if version is not None:
        (app / "Contents" / "Resources" / "splash" / "release.json").write_text(
            json.dumps({"version": version})
        )
    return app


def test_the_splash_app_is_found_by_its_bundle_id(tmp_path: Path) -> None:
    app = make_app(tmp_path / "a", "1.5.0")
    assert desktop_app.find_splash_app((app,)) == desktop_app.SplashApp(app, "1.5.0")
    assert desktop_app.find_splash_app((make_app(tmp_path / "b", None),)).version == "9.9"  # type: ignore[union-attr]
    other = make_app(tmp_path / "c", "1.5.0", bundle_id="com.example.splash")
    assert desktop_app.find_splash_app((other,)) is None
    assert desktop_app.find_splash_app((tmp_path / "missing.app",)) is None


def doctor_check(harness: Any) -> dict[str, Any] | None:
    checks = harness.client.post("/api/admin/doctor").json()["checks"]
    return next((c for c in checks if c["id"] == "splash_app"), None)


def test_doctor_names_the_splash_app_and_a_release_mismatch(
    harness_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = harness_factory(installed=())
    assert doctor_check(harness) is None, "no app, no line"
    monkeypatch.setattr(desktop_app, "CANDIDATES", (make_app(tmp_path / "same", "1.6.0"),))
    same = doctor_check(harness)
    assert same is not None and same["status"] == "ok"
    assert "the same release as the engine" in same["message"]
    monkeypatch.setattr(desktop_app, "CANDIDATES", (make_app(tmp_path / "old", "1.4.1"),))
    old = doctor_check(harness)
    assert old is not None and old["status"] == "warn"
    assert "Splash app 1.4.1 and the Splash 1.6.0 engine" in old["message"]
    assert "brew upgrade --cask incoai/tap/splash-app" in old["fix"]

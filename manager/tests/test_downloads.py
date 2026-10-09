"""The download pipeline end to end against the fake engine.

These are the only tests that drive the real code path: the manager spawns the
fake `install/models.py` as a subprocess, reads its output, and watches the
Hugging Face cache grow. Pause, resume and cancel are therefore observed for
what they are — a signal to a child process and a decision about which partial
blobs survive — not a state flag flipped in isolation.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import pytest

from .fakeengine import MODEL, MODEL_27B

GGUF = "unsloth/Qwen3.6-35B-A3B-GGUF"
GGUF_27B = "prism-ml/Ternary-Bonsai-2-27B-gguf"
MLX = "mlx-community/Qwen3.6-35B-A3B-4bit"
MLX_8BIT = "mlx-community/Qwen3.6-35B-A3B-8bit"
GGUF_NO_VISION = "unsloth/Qwen3.6-35B-A3B-GGUF-textonly"

# Slow enough to pause and cancel mid-file, fast enough for a test suite.
SLOW = {
    "FAKE_SPLASH_DL_BPS": "256K",
    "FAKE_SPLASH_DL_SHARD_BYTES": "2M",
    "FAKE_SPLASH_DL_VISION_BYTES": "1M",
}


@pytest.fixture
def hub_harness(harness_factory, fake_hub, monkeypatch):
    monkeypatch.setenv("HF_ENDPOINT", fake_hub)

    def make(env: dict[str, str] | None = None, **kwargs):
        harness = harness_factory(
            installed=(), env={"HF_ENDPOINT": fake_hub, **(env or {})}, **kwargs
        )
        harness.patch_settings({"global": {"hf": {"endpoint": fake_hub}}})
        return harness

    return make


def queue(harness, model: str, **body: Any) -> dict[str, Any]:
    response = harness.client.post("/api/admin/downloads", json={"id": model, **body})
    assert response.status_code == 201, response.text
    return dict(response.json())


def item(harness, dl: str) -> dict[str, Any]:
    response = harness.client.get("/api/admin/downloads")
    assert response.status_code == 200, response.text
    found = {row["id"]: row for row in response.json()["items"]}
    assert dl in found, f"download {dl} vanished from the queue"
    return dict(found[dl])


def wait_for(harness, dl: str, *states: str, timeout: float = 40.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = item(harness, dl)
    while last["state"] not in states:
        if time.monotonic() > deadline:
            raise AssertionError(f"download stayed in {last['state']}, wanted {states}: {last}")
        time.sleep(0.1)
        last = item(harness, dl)
    return last


def wait_for_progress(
    harness, dl: str, minimum: float = 0.05, timeout: float = 40.0
) -> dict[str, Any]:
    """Wait until some bytes have landed, so a pause really interrupts work."""
    deadline = time.monotonic() + timeout
    last = item(harness, dl)
    while (last["progress"] or 0) < minimum:
        if time.monotonic() > deadline:
            raise AssertionError(f"download never reached {minimum} progress: {last}")
        time.sleep(0.1)
        last = item(harness, dl)
    return last


def incomplete_blobs(harness) -> list[Path]:
    directory = harness.state.settings.models_dir()
    return sorted(directory.glob("models--*/blobs/*.incomplete"))


def finished_blobs(harness) -> list[Path]:
    directory = harness.state.settings.models_dir()
    return [
        path
        for path in directory.glob("models--*/blobs/*")
        if path.is_file() and not path.name.endswith(".incomplete")
    ]


def test_a_download_runs_to_done_and_lands_in_the_cache(hub_harness):
    harness = hub_harness()
    started = queue(harness, MODEL)
    assert started["state"] in ("queued", "running")
    assert started["bytes_total"], "the expected size is worked out before starting"
    assert started["files"], "the exact file set comes from the compatibility check"

    done = wait_for(harness, started["id"], "done")
    assert done["progress"] == 1.0
    assert done["bytes_done"] == done["bytes_total"]
    assert done["error"] is None
    assert finished_blobs(harness), "weights must land in the Hugging Face cache"
    assert not incomplete_blobs(harness), "a completed download leaves no .incomplete blob"


def test_a_finished_download_raises_the_done_alert(hub_harness):
    """ "a notification when done"."""
    harness = hub_harness()
    started = queue(harness, MODEL)
    wait_for(harness, started["id"], "done")
    alerts = harness.client.get("/api/admin/alerts").json()["alerts"]
    done = [a for a in alerts if a["id"] == f"download_done:{started['id']}"]
    assert done, f"expected a download_done alert, got {[a['id'] for a in alerts]}"
    assert done[0]["actions"], "the alert offers to load the model"


def verify_lines(log: list[str]) -> list[int]:
    return [i for i, line in enumerate(log) if "preflight passed" in line]


def test_auto_verify_runs_a_quick_check_after_prepare(hub_harness):
    """Auto-verify: after `prepare` succeeds, `models.py … verify` runs, and
    the quick check is the default (no `--full`)."""
    harness = hub_harness()
    started = queue(harness, MODEL)
    assert started["verify"] is True
    done = wait_for(harness, started["id"], "done")
    log = done["log_tail"]
    found = verify_lines(log)
    assert len(found) == 1, log
    assert "preflight passed (quick)" in log[found[0]], log[found[0]]
    prepared = [i for i, line in enumerate(log) if "Selected" in line]
    assert prepared and max(prepared) < found[0], "verify runs after the installer has prepared"


def test_the_full_verify_setting_adds_full_to_the_auto_verify(hub_harness):
    """The "Full verify" setting adds `--full` to the verify step only."""
    harness = hub_harness()
    harness.patch_settings({"global": {"downloads": {"full_verify": True}}})
    started = queue(harness, MODEL)
    done = wait_for(harness, started["id"], "done")
    found = verify_lines(done["log_tail"])
    assert len(found) == 1, done["log_tail"]
    assert "preflight passed (full)" in done["log_tail"][found[0]], done["log_tail"][found[0]]


def test_a_download_without_verify_skips_the_check(hub_harness):
    harness = hub_harness()
    started = queue(harness, MODEL, verify=False)
    done = wait_for(harness, started["id"], "done")
    assert verify_lines(done["log_tail"]) == [], done["log_tail"]


def test_a_refused_auto_verify_fails_the_download(hub_harness):
    """A model shows Ready only after verify. A refused verify fails the job
    with the installer's message and a `download_failed` alert, and the job is not done."""
    harness = hub_harness({"FAKE_SPLASH_VERIFY_FAIL": "1"})
    started = queue(harness, MODEL)
    failed = wait_for(harness, started["id"], "failed")
    assert failed["error"]["code"] == "installer_failed", failed["error"]
    assert failed["error"]["action"] == "retry"
    assert "source content hash mismatch" in failed["error"]["message"]
    assert verify_lines(failed["log_tail"]) == [], "the fake refused, so nothing passed"
    alerts = harness.client.get("/api/admin/alerts").json()["alerts"]
    assert [a for a in alerts if a["id"] == f"download_failed:{started['id']}"], [
        a["id"] for a in alerts
    ]
    assert not [a for a in alerts if a["id"] == f"download_done:{started['id']}"]


def test_progress_reports_bytes_speed_and_a_log_tail(hub_harness):
    """ "live speed/ETA/per-file progress"."""
    harness = hub_harness(SLOW)
    started = queue(harness, MODEL)
    running = wait_for_progress(harness, started["id"], 0.05)
    assert running["state"] == "running"
    assert running["bytes_done"] > 0
    seen_speed = False
    seen_file = False
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and running["state"] == "running":
        running = item(harness, started["id"])
        seen_speed = seen_speed or bool(running.get("speed_bps"))
        seen_file = seen_file or any(f["state"] == "downloading" for f in running["files"])
        time.sleep(0.1)
    assert seen_speed, "a live download must report a speed"
    assert seen_file, "per-file progress must be reported"


def _in_flight_file(harness, dl: str, timeout: float = 30.0) -> dict[str, Any]:
    """Poll until the queue reports a file mid-download (bytes > 0, not finished)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = item(harness, dl)
        for file in row["files"]:
            size = file["size_bytes"] or 0
            if file["state"] == "downloading" and 0 < file["done_bytes"] < size:
                return row
        if row["state"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    raise AssertionError(f"no file was ever reported mid-download: {item(harness, dl)}")


@pytest.mark.parametrize("hub", ["1.28", "legacy"])
def test_progress_reads_the_partial_file_while_it_grows(hub_harness, hub):
    """Acceptance 2026-10-04: huggingface_hub 1.28 (bundled with Splash 1.2.0) writes
    `<etag>.<uuid8>.incomplete`; progress froze until each file finished. The old
    `<etag>.incomplete` name must keep working."""
    env = dict(SLOW)
    if hub == "legacy":
        env["FAKE_SPLASH_DL_HUB"] = "legacy"
    harness = hub_harness(env)
    started = queue(harness, MODEL)
    row = _in_flight_file(harness, started["id"])
    finished = sum(p.stat().st_size for p in finished_blobs(harness))
    assert row["bytes_done"] > finished, "the growing partial file counts toward progress"
    names = [p.name for p in incomplete_blobs(harness)]
    pattern = r"[0-9a-f]+\.[0-9a-f]{8}\.incomplete" if hub == "1.28" else r"[0-9a-f]+\.incomplete"
    assert names and all(re.fullmatch(pattern, n) for n in names), names
    harness.client.delete(f"/api/admin/downloads/{started['id']}")


def test_resume_discards_the_partial_the_paused_run_left(hub_harness):
    """Hub 1.28 cannot continue a partial file: each run writes a new one. The stale one
    is deleted when the download resumes, so progress is honest and no disk leaks."""
    harness = hub_harness(SLOW)
    started = queue(harness, MODEL)
    _in_flight_file(harness, started["id"])
    harness.client.post(f"/api/admin/downloads/{started['id']}/pause")
    stale = incomplete_blobs(harness)
    assert stale, "sanity: the paused run left its partial file"
    harness.client.post(f"/api/admin/downloads/{started['id']}/resume")
    _in_flight_file(harness, started["id"])
    assert not any(p.exists() for p in stale), "the paused run's partial is gone"
    harness.client.delete(f"/api/admin/downloads/{started['id']}")


def test_pause_stops_the_child_and_keeps_the_partial_blob(hub_harness):
    harness = hub_harness(SLOW)
    started = queue(harness, MODEL)
    wait_for_progress(harness, started["id"], 0.05)
    response = harness.client.post(f"/api/admin/downloads/{started['id']}/pause")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "paused"

    partials = incomplete_blobs(harness)
    assert partials, "pause must keep the partial blob so the download can resume"

    done_at = item(harness, started["id"])["bytes_done"]
    time.sleep(1.5)
    after = item(harness, started["id"])
    assert after["state"] == "paused"
    assert after["bytes_done"] == done_at, "a paused download must not keep moving"
    assert not harness.state.downloads.processes, "the installer subprocess is gone"


def test_resume_finishes_the_download(hub_harness):
    harness = hub_harness(SLOW)
    started = queue(harness, MODEL)
    wait_for_progress(harness, started["id"], 0.05)
    harness.client.post(f"/api/admin/downloads/{started['id']}/pause")
    paused = item(harness, started["id"])
    assert paused["state"] == "paused"

    response = harness.client.post(f"/api/admin/downloads/{started['id']}/resume")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "queued"
    done = wait_for(harness, started["id"], "done")
    assert done["bytes_done"] == done["bytes_total"]
    assert not incomplete_blobs(harness)


def test_state_changes_go_out_as_download_state_and_samples_as_progress(hub_harness):
    """A transition is `download.state`, a sample is `download.progress`, and
    a sample never carries a state that no `download.state` event announced first."""
    harness = hub_harness(SLOW)
    seen: list[tuple[str, dict[str, Any]]] = []
    harness.state.events.listeners.append(
        lambda event, data: seen.append((event, data)) if event.startswith("download.") else None
    )
    started = queue(harness, MODEL)
    dl = started["id"]
    wait_for_progress(harness, dl, 0.05)
    harness.client.post(f"/api/admin/downloads/{dl}/pause")
    harness.client.post(f"/api/admin/downloads/{dl}/resume")
    wait_for(harness, dl, "done")

    mine = [(event, data) for event, data in seen if data["id"] == dl]
    states = [data["state"] for event, data in mine if event == "download.state"]
    assert states[0] == "queued" and states[-1] == "done", states
    assert {"running", "paused"} <= set(states), states
    announced = None
    for event, data in mine:
        if event == "download.state":
            announced = data["state"]
        else:
            assert event == "download.progress", event
            assert data["state"] == announced, (announced, data["state"])
    assert any(event == "download.progress" for event, _ in mine)


def test_cancel_removes_the_blobs_it_started(hub_harness):
    harness = hub_harness(SLOW)
    started = queue(harness, MODEL)
    _in_flight_file(harness, started["id"])
    assert incomplete_blobs(harness), "sanity: a partial blob exists while downloading"
    # A second run (pause → resume) leaves a stale per-process partial too.
    harness.client.post(f"/api/admin/downloads/{started['id']}/pause")
    harness.client.post(f"/api/admin/downloads/{started['id']}/resume")
    _in_flight_file(harness, started["id"])

    response = harness.client.delete(f"/api/admin/downloads/{started['id']}")
    assert response.status_code == 204, response.text
    assert item(harness, started["id"])["state"] == "cancelled"
    assert not incomplete_blobs(harness), "cancel discards this download's partial blobs"
    time.sleep(1.0)
    assert not incomplete_blobs(harness), "and nothing reappears afterwards"


def test_cancel_leaves_partials_that_existed_before_queueing(hub_harness):
    from splash_gui.schemas import DownloadFile, DownloadItem

    harness = hub_harness()
    downloads = harness.state.downloads
    blobs = harness.state.settings.models_dir() / "models--o--r" / "blobs"
    blobs.mkdir(parents=True)
    theirs = blobs / "abc.1234abcd.incomplete"
    ours = [blobs / "abc.incomplete", blobs / "abc.deadbeef.incomplete"]
    for path in (theirs, *ours):
        path.write_bytes(b"x")
    downloads.items["dl"] = DownloadItem(
        id="dl",
        model="o/r",
        state="paused",
        created_at="2026-10-04T00:00:00Z",
        files=[DownloadFile(name="a.safetensors", repo_id="o/r")],
    )
    downloads.blobs = {"dl": {"o/r/a.safetensors": "abc"}}
    downloads.preexisting = {"dl": [str(theirs)]}
    assert harness.client.delete("/api/admin/downloads/dl").status_code == 204
    assert theirs.exists(), "a partial that was there before queueing is not ours"
    assert not any(p.exists() for p in ours)


def test_cancel_keeps_files_when_asked(hub_harness):
    harness = hub_harness(SLOW)
    started = queue(harness, MODEL)
    wait_for_progress(harness, started["id"], 0.05)
    response = harness.client.delete(f"/api/admin/downloads/{started['id']}?keep_files=true")
    assert response.status_code == 204, response.text
    assert item(harness, started["id"])["state"] == "cancelled"
    assert incomplete_blobs(harness), "keep_files leaves the partial blobs for a later retry"


def test_cancel_does_not_delete_a_blob_another_download_shares(hub_harness):
    """Cancelling one model must never destroy a file another queue entry needs."""
    harness = hub_harness(SLOW)
    first = queue(harness, MODEL)
    wait_for_progress(harness, first["id"], 0.05)
    first_blobs = {p.name for p in incomplete_blobs(harness)}
    assert first_blobs

    # The draft repo is a separate queue entry's file set, but the blob is
    # recorded against the download that fetched it. Queue the 27B model too, so
    # the shared draft blob has a second claimant.
    second = queue(harness, MODEL_27B)
    time.sleep(1.0)

    harness.client.delete(f"/api/admin/downloads/{first['id']}")
    remaining = {p.name for p in incomplete_blobs(harness)}
    still_claimed = set(harness.state.downloads.blobs.get(second["id"], {}).values())
    for name in still_claimed:
        if name in first_blobs:
            assert name in remaining, f"cancel removed a blob {second['id']} still needs"

    harness.client.post(f"/api/admin/downloads/{second['id']}/pause")
    harness.client.delete(f"/api/admin/downloads/{second['id']}")


def test_cancel_spares_shared_blobs_and_tolerates_orphan_blob_maps(hub_harness):
    """The test above passes vacuously when the two models share no blob (their
    drafts differ), so the rule is pinned here with a blob two entries claim, plus
    a blob map left behind by a refused queue request (it once raised KeyError)."""
    from splash_gui.schemas import DownloadFile, DownloadItem

    harness = hub_harness()
    downloads = harness.state.downloads
    blobs = harness.state.settings.models_dir() / "models--o--r" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "shared.incomplete").write_bytes(b"x")
    (blobs / "own.incomplete").write_bytes(b"y")
    files = [DownloadFile(name="a.safetensors", repo_id="o/r")]
    for dl in ("first", "second"):
        downloads.items[dl] = DownloadItem(
            id=dl, model="o/r", state="paused", created_at="2026-10-04T00:00:00Z", files=files
        )
    downloads.items["first"].files = [*files, DownloadFile(name="b.safetensors", repo_id="o/r")]
    downloads.blobs = {
        "first": {"o/r/a.safetensors": "shared", "o/r/b.safetensors": "own"},
        "second": {"o/r/a.safetensors": "shared"},
        "refused-before-queueing": {"o/r/c.safetensors": "other"},
    }
    response = harness.client.delete("/api/admin/downloads/first")
    assert response.status_code == 204, response.text
    assert (blobs / "shared.incomplete").exists(), "still claimed by the second download"
    assert not (blobs / "own.incomplete").exists()


def test_a_refused_queue_request_leaves_no_blob_map(hub_harness, monkeypatch):
    import shutil

    harness = hub_harness()
    usage = shutil.disk_usage(harness.home)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage._replace(free=10**9))
    response = harness.client.post("/api/admin/downloads", json={"id": f"{GGUF}:UD-Q4_K_M"})
    assert response.status_code == 507, response.text
    assert harness.state.downloads.blobs == {}


def test_a_gated_repository_fails_with_an_actionable_code(hub_harness, monkeypatch):
    """A gated repo with no token is reported as an auth problem, not a crash."""
    monkeypatch.delenv("HF_TOKEN", raising=False)
    harness = hub_harness({"FAKE_SPLASH_DL_FAIL": "gated"})
    started = queue(harness, MODEL)
    failed = wait_for(harness, started["id"], "failed")
    assert failed["error"]["code"] == "gated"
    assert "HF_TOKEN" in failed["error"]["message"] or "auth" in failed["error"]["message"].lower()
    assert failed["error"]["action"] == "add_hf_token", "the hint follows the code"


def test_a_gated_repository_succeeds_with_a_token(hub_harness, monkeypatch):
    """The token makes the same repository work."""
    monkeypatch.setenv("HF_TOKEN", "hf_test_token")
    harness = hub_harness({"FAKE_SPLASH_DL_FAIL": "gated"})
    started = queue(harness, MODEL)
    done = wait_for(harness, started["id"], "done")
    assert done["error"] is None


def test_a_network_failure_is_reported_and_can_be_retried(hub_harness):
    harness = hub_harness({"FAKE_SPLASH_DL_FAIL": "network_mid"})
    started = queue(harness, MODEL)
    failed = wait_for(harness, started["id"], "failed")
    assert failed["error"]["code"] == "hub_unreachable"
    assert failed["error"]["action"] == "retry"
    assert failed["error"]["message"]
    # resume clears the error and retries.
    response = harness.client.post(f"/api/admin/downloads/{started['id']}/resume")
    assert response.status_code == 200, response.text
    assert response.json()["error"] is None


def test_a_disk_full_failure_is_classified(hub_harness):
    harness = hub_harness({"FAKE_SPLASH_DL_FAIL": "disk_full"})
    started = queue(harness, MODEL)
    failed = wait_for(harness, started["id"], "failed")
    # ENOSPC ("No space left on device") must not fall through to installer_failed.
    assert failed["error"]["code"] == "disk_full"
    assert failed["error"]["action"] == "free_space"
    assert failed["error"]["needed_bytes"] >= 2 * 1024**3 and failed["error"]["free_bytes"] > 0


def test_a_failed_download_raises_an_alert(hub_harness):
    harness = hub_harness({"FAKE_SPLASH_DL_FAIL": "network"})
    started = queue(harness, MODEL)
    wait_for(harness, started["id"], "failed")
    alerts = harness.client.get("/api/admin/alerts").json()["alerts"]
    assert any(a["id"] == f"download_failed:{started['id']}" for a in alerts)


def test_an_incompatible_model_is_refused_before_any_download(hub_harness):
    """Compatibility is checked before download, so nothing is fetched."""
    harness = hub_harness()
    response = harness.client.post("/api/admin/downloads", json={"id": MLX_8BIT})
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "incompatible"
    assert body["message"], "the reason from the compatibility check is shown"
    assert harness.client.get("/api/admin/downloads").json()["items"] == []


def test_a_model_without_a_usable_projector_needs_language_only(hub_harness):
    """A repository with no usable projector is refused unless
    the user asks for text only, and the reason says so."""
    harness = hub_harness()
    response = harness.client.post("/api/admin/downloads", json={"id": GGUF_NO_VISION})
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "vision_unavailable"
    assert "language-only" in body["message"] or "projector" in body["message"]

    started = queue(harness, GGUF_NO_VISION, language_only=True)
    wait_for(harness, started["id"], "done")


def test_a_text_only_model_is_reported_as_such_not_as_incompatible(hub_harness):
    """The badge: "Compatible — text only"."""
    harness = hub_harness()
    response = harness.client.post(f"/api/admin/inspect?id={GGUF_NO_VISION}&refresh=1")
    assert response.status_code == 200, response.text
    result = dict(response.json())
    assert result["compatible"] is True
    assert result["badge"] == "text_only"
    assert result["vision"]["available"] is False
    assert result["vision"]["reason"]


def test_queuing_the_same_model_twice_is_a_conflict(hub_harness):
    harness = hub_harness(SLOW)
    first = queue(harness, MODEL)
    wait_for_progress(harness, first["id"], 0.02)
    response = harness.client.post("/api/admin/downloads", json={"id": MODEL})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "already_queued"


def test_updating_a_model_that_is_queued_is_the_same_conflict(
    harness_factory, fake_hub, monkeypatch
):
    """`POST /models/{id}/update` reruns the queue, so it answers the same code."""
    monkeypatch.setenv("HF_ENDPOINT", fake_hub)
    harness = harness_factory(installed=(MODEL,), env={"HF_ENDPOINT": fake_hub, **SLOW})
    harness.patch_settings({"global": {"hf": {"endpoint": fake_hub}}})
    first = queue(harness, MODEL)
    wait_for_progress(harness, first["id"], 0.02)
    response = harness.client.post(f"/api/admin/models/{MODEL}/update")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "already_queued"


def test_a_short_model_id_is_rejected(hub_harness):
    """Full Hugging Face IDs only, no short aliases."""
    harness = hub_harness()
    response = harness.client.post("/api/admin/downloads", json={"id": "qwen3.6-35b"})
    assert response.status_code == 400, response.text


def test_the_queue_is_bounded_by_the_parallel_setting(hub_harness):
    harness = hub_harness(SLOW)
    harness.patch_settings({"global": {"downloads": {"parallel": 1}}})
    first = queue(harness, MODEL)
    second = queue(harness, MODEL_27B)
    wait_for_progress(harness, first["id"], 0.02)
    assert len(harness.state.downloads.tasks) == 1, "parallel=1 runs one installer at a time"
    assert item(harness, second["id"])["state"] == "queued"
    harness.client.post(f"/api/admin/downloads/{first['id']}/pause")


def test_pausing_a_finished_download_is_refused(hub_harness):
    harness = hub_harness()
    started = queue(harness, MODEL)
    wait_for(harness, started["id"], "done")
    response = harness.client.post(f"/api/admin/downloads/{started['id']}/pause")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "invalid_download_state"


def test_resuming_a_running_download_is_refused(hub_harness):
    harness = hub_harness(SLOW)
    started = queue(harness, MODEL)
    wait_for_progress(harness, started["id"], 0.02)
    response = harness.client.post(f"/api/admin/downloads/{started['id']}/resume")
    assert response.status_code == 409, response.text
    harness.client.post(f"/api/admin/downloads/{started['id']}/pause")


def test_an_unknown_download_is_a_404(hub_harness):
    harness = hub_harness()
    for method, path in (
        ("post", "/api/admin/downloads/nope/pause"),
        ("post", "/api/admin/downloads/nope/resume"),
        ("delete", "/api/admin/downloads/nope"),
    ):
        response = getattr(harness.client, method)(path)
        assert response.status_code == 404, f"{method} {path} -> {response.status_code}"


async def test_the_queue_survives_a_manager_restart(hub_harness):
    """Downloads.json is the persisted queue, and an interrupted
    run comes back as `queued` rather than `running`."""
    from splash_gui.downloads.service import Downloads

    harness = hub_harness(SLOW)
    started = queue(harness, MODEL)
    wait_for_progress(harness, started["id"], 0.05)
    harness.client.post(f"/api/admin/downloads/{started['id']}/pause")
    assert harness.state.paths.downloads_file.exists()

    # A fresh service over the same home is what the next launch builds.
    revived = Downloads(harness.state)
    revived.stopping = True  # do not actually resume the installer
    await revived.start()

    assert started["id"] in revived.items
    assert revived.items[started["id"]].state == "paused"
    assert revived.blobs, "the blob map is persisted so resume can find partial files"
    assert revived.preexisting, "and so cancel knows what it did not create"


async def test_an_interrupted_running_download_returns_as_queued(hub_harness):
    """A manager that dies mid-download must not leave the item stuck on
    `running`; it is requeued instead."""
    from splash_gui.downloads.service import Downloads

    harness = hub_harness(SLOW)
    started = queue(harness, MODEL)
    wait_for_progress(harness, started["id"], 0.05)

    revived = Downloads(harness.state)
    revived.stopping = True
    await revived.start()
    assert revived.items[started["id"]].state in ("running", "queued")
    revived.items[started["id"]].state = "running"
    revived.save()

    again = Downloads(harness.state)
    again.stopping = True
    await again.start()
    assert again.items[started["id"]].state == "queued"
    harness.client.post(f"/api/admin/downloads/{started['id']}/pause")


def test_language_only_downloads_no_projector(hub_harness):
    harness = hub_harness()
    started = queue(harness, GGUF, language_only=True)
    done = wait_for(harness, started["id"], "done")
    names = [f["name"] for f in done["files"]]
    assert not any("mmproj" in name for name in names), names
    assert any(name.endswith(".gguf") for name in names)


def test_the_mlx_repo_downloads_every_shard(hub_harness):
    harness = hub_harness()
    started = queue(harness, MLX)
    done = wait_for(harness, started["id"], "done")
    names = [f["name"] for f in done["files"]]
    assert any(name.endswith(".safetensors") for name in names), names
    assert "config.json" in names


def test_a_draft_repo_is_downloaded_too(hub_harness):
    """The draft repo's config and weights are part of the download."""
    harness = hub_harness()
    started = queue(harness, MLX)
    done = wait_for(harness, started["id"], "done")
    repos = {f["repo_id"] for f in done["files"]}
    assert MLX in repos
    assert "incoai/Qwen3.6-35B-A3B-DFlash2" in repos, repos


def test_a_download_that_does_not_fit_on_disk_is_refused_up_front(hub_harness, monkeypatch):
    """Disk check: remaining bytes plus a 2 GB margin, before queuing."""
    import shutil

    harness = hub_harness()
    usage = shutil.disk_usage(harness.home)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage._replace(free=10**9))
    response = harness.client.post("/api/admin/downloads", json={"id": f"{GGUF}:UD-Q4_K_M"})
    assert response.status_code == 507, response.text
    assert response.json()["error"]["code"] == "disk_full"
    assert harness.client.get("/api/admin/downloads").json()["items"] == []


# --- Cancel removes what the job created -------------------------------

REPO_DIR = "models--o--r"


def _snapshot(harness, repo: str, rev: str, name: str, digest: str) -> tuple[Path, Path]:
    """A complete blob and the relative snapshot link to it, as huggingface_hub writes them."""
    import os

    folder = harness.state.settings.models_dir() / ("models--" + repo.replace("/", "--"))
    blob = folder / "blobs" / digest
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"weights")
    link = folder / "snapshots" / rev / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(blob, link.parent))
    return blob, link


def _job(harness, dl: str, files: list[tuple[str, str]], digests, preexisting=()) -> None:
    from splash_gui.schemas import DownloadFile, DownloadItem

    downloads = harness.state.downloads
    downloads.items[dl] = DownloadItem(
        id=dl,
        model="o/r",
        state="paused",
        created_at="2026-10-07T00:00:00Z",
        files=[DownloadFile(name=name, repo_id=repo) for repo, name in files],
    )
    downloads.blobs[dl] = {f"{repo}/{name}": digests[name] for repo, name in files}
    downloads.preexisting[dl] = [str(p) for p in preexisting]


def test_cancel_removes_the_snapshot_links_and_blobs_the_job_created(hub_harness):
    harness = hub_harness()
    blob, link = _snapshot(harness, "o/r", "c" * 40, "model.safetensors", "a" * 64)
    _job(harness, "dl", [("o/r", "model.safetensors")], {"model.safetensors": "a" * 64})
    response = harness.client.delete("/api/admin/downloads/dl")
    assert response.status_code == 204, response.text
    assert not link.is_symlink() and not blob.exists()
    assert not (blob.parent.parent / "snapshots" / ("c" * 40)).exists(), (
        "an emptied snapshot folder goes"
    )


def test_keep_files_keeps_the_snapshot_files(hub_harness):
    harness = hub_harness()
    blob, link = _snapshot(harness, "o/r", "c" * 40, "model.safetensors", "a" * 64)
    _job(harness, "dl", [("o/r", "model.safetensors")], {"model.safetensors": "a" * 64})
    response = harness.client.delete("/api/admin/downloads/dl?keep_files=true")
    assert response.status_code == 204
    assert link.is_symlink() and blob.exists()


def test_cancel_keeps_what_existed_before_queueing_or_is_shared(hub_harness, monkeypatch):
    """Four blobs the job fetched or claims, each kept for a different reason: it was
    there before the job (preexisting), another repository links it, another unfinished
    download claims it, or an installed model uses it. Only the fifth goes."""
    from splash_gui.downloads import service as downloads_service

    harness = hub_harness()
    old = _snapshot(harness, "o/r", "c" * 40, "old.bin", "1" * 64)
    other_repo = _snapshot(harness, "o/r", "c" * 40, "mirror.bin", "2" * 64)
    # Another repository's snapshot linking the same blob (the draft is shared this way).
    import os

    mirror = harness.state.settings.models_dir() / "models--p--q" / "snapshots" / ("d" * 40)
    mirror.mkdir(parents=True)
    (mirror / "mirror.bin").symlink_to(os.path.relpath(other_repo[0], mirror))
    claimed = _snapshot(harness, "o/r", "c" * 40, "claimed.bin", "3" * 64)
    installed = _snapshot(harness, "o/r", "c" * 40, "installed.bin", "4" * 64)
    gone = _snapshot(harness, "o/r", "c" * 40, "gone.bin", "5" * 64)
    files = [
        ("o/r", "old.bin"),
        ("o/r", "mirror.bin"),
        ("o/r", "claimed.bin"),
        ("o/r", "installed.bin"),
        ("o/r", "gone.bin"),
    ]
    digests = {
        "old.bin": "1" * 64,
        "mirror.bin": "2" * 64,
        "claimed.bin": "3" * 64,
        "installed.bin": "4" * 64,
        "gone.bin": "5" * 64,
    }
    _job(harness, "dl", files, digests, preexisting=[old[0]])
    _job(harness, "dl2", [("o/r", "claimed.bin")], {"claimed.bin": "3" * 64})
    harness.state.downloads.items["dl2"].state = "queued"

    class Selection:
        def real_paths(self):
            return {installed[0]}

    monkeypatch.setattr(downloads_service, "read_all", lambda root: [Selection()])
    response = harness.client.delete("/api/admin/downloads/dl")
    assert response.status_code == 204, response.text
    for blob, link in (old, other_repo, claimed, installed):
        assert blob.exists() and link.is_symlink(), f"{blob.name} must survive"
    assert not gone[0].exists() and not gone[1].is_symlink(), "only the job's own file goes"


def test_a_job_with_no_record_of_its_files_removes_no_snapshot(hub_harness):
    harness = hub_harness()
    blob, link = _snapshot(harness, "o/r", "c" * 40, "model.safetensors", "a" * 64)
    _job(harness, "dl", [("o/r", "model.safetensors")], {"model.safetensors": "a" * 64})
    del harness.state.downloads.preexisting["dl"]
    harness.client.delete("/api/admin/downloads/dl")
    assert link.is_symlink() and blob.exists(), "queued before the record existed: keep"


async def test_a_persisted_blob_id_that_is_not_a_hub_hash_is_dropped_on_load(hub_harness, caplog):
    """Cancel joins a persisted blob ID into the blobs folder and unlinks what it
    finds, so an ID such as `../../x` (written before the Hub's answer was checked) would
    remove `models/x`. Load drops such an entry and logs it; its file has no blob to remove."""
    import json
    import logging

    from splash_gui.downloads.service import Downloads

    harness = hub_harness()
    models = harness.state.settings.models_dir()
    models.mkdir(parents=True, exist_ok=True)
    victim = models / "x"  # what models--o--r/blobs/../../x names
    victim.write_bytes(b"not a blob")
    good = "b" * 64
    harness.state.paths.downloads_file.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "dl",
                        "model": "o/r",
                        "state": "paused",
                        "created_at": "2026-10-07T00:00:00Z",
                        "files": [
                            {"name": "model.safetensors", "repo_id": "o/r", "resumable": True},
                            {"name": "config.json", "repo_id": "o/r"},
                        ],
                    }
                ],
                "blobs": {"dl": {"o/r/model.safetensors": "../../x", "o/r/config.json": good}},
                "preexisting": {"dl": []},
            }
        )
    )

    revived = Downloads(harness.state)
    revived.stopping = True
    with caplog.at_level(logging.WARNING, logger="splash_gui.downloads.service"):
        await revived.start()

    assert revived.blobs == {"dl": {"o/r/config.json": good}}
    assert "'../../x' is not a Hub blob ID" in caplog.text
    assert revived.items["dl"].files[0].resumable is False
    await revived.cancel("dl", keep_files=False)
    assert victim.read_bytes() == b"not a blob", "cancel must not unlink outside the blobs folder"


def test_a_local_draft_is_a_queue_item_that_fetches_only_its_repo(hub_harness):
    """The drop-in's draft is an ordinary Downloads item of kind `draft`.
    It fetches the draft repo's JSON and weights, runs no installer, and leaves a cache
    the drop-in reads as complete."""
    from splash_gui.hubcache import repo_folder
    from splash_gui.models.local import LocalModels

    harness = hub_harness()
    draft = "incoai/Qwen3.6-35B-A3B-DFlash2"
    queued = harness.client.portal.call(harness.state.downloads.queue_draft, draft)
    assert queued.kind == "draft" and queued.verify is False
    done = wait_for(harness, queued.id, "done")
    assert done["kind"] == "draft" and done["model"] == draft
    assert {f["repo_id"] for f in done["files"]} == {draft}, done["files"]
    assert any(f["name"].endswith(".safetensors") for f in done["files"])
    folder = repo_folder(harness.state.settings.models_dir(), draft)
    assert LocalModels._draft_complete(folder)
    assert not harness.state.downloads.active_model(draft)
    assert not any("--model" in line for line in done["log_tail"]), (
        "a draft never goes through Splash's installer"
    )


def test_a_model_download_still_reports_its_kind(hub_harness):
    harness = hub_harness()
    started = queue(harness, MLX)
    assert started["kind"] == "model"

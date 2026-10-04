"""The download pipeline end to end against the fake engine (SPEC §9.4, D9).

These are the only tests that drive the real code path: the manager spawns the
fake `install/models.py` as a subprocess, reads its output, and watches the
Hugging Face cache grow. Pause, resume and cancel are therefore observed for
what they are — a signal to a child process and a decision about which partial
blobs survive — not a state flag flipped in isolation.
"""

from __future__ import annotations

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
    assert started["bytes_total"], "the expected size is worked out before starting (SPEC §9.4)"
    assert started["files"], "the exact file set comes from the compatibility check"

    done = wait_for(harness, started["id"], "done")
    assert done["progress"] == 1.0
    assert done["bytes_done"] == done["bytes_total"]
    assert done["error"] is None
    assert finished_blobs(harness), "weights must land in the Hugging Face cache"
    assert not incomplete_blobs(harness), "a completed download leaves no .incomplete blob"


def test_a_finished_download_raises_the_done_alert(hub_harness):
    """D9: "a notification when done"."""
    harness = hub_harness()
    started = queue(harness, MODEL)
    wait_for(harness, started["id"], "done")
    alerts = harness.client.get("/api/admin/alerts").json()["alerts"]
    done = [a for a in alerts if a["id"] == f"download_done:{started['id']}"]
    assert done, f"expected a download_done alert, got {[a['id'] for a in alerts]}"
    assert done[0]["actions"], "the alert offers to load the model"


def test_progress_reports_bytes_speed_and_a_log_tail(hub_harness):
    """D9: "live speed/ETA/per-file progress"."""
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


def test_cancel_removes_the_blobs_it_started(hub_harness):
    harness = hub_harness(SLOW)
    started = queue(harness, MODEL)
    wait_for_progress(harness, started["id"], 0.05)
    assert incomplete_blobs(harness), "sanity: a partial blob exists while downloading"

    response = harness.client.delete(f"/api/admin/downloads/{started['id']}")
    assert response.status_code == 204, response.text
    assert item(harness, started["id"])["state"] == "cancelled"
    assert not incomplete_blobs(harness), "cancel discards this download's partial blobs"
    time.sleep(1.0)
    assert not incomplete_blobs(harness), "and nothing reappears afterwards"


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
    assert failed["error"]["action"] == "retry"


def test_a_gated_repository_succeeds_with_a_token(hub_harness, monkeypatch):
    """D10: the token makes the same repository work."""
    monkeypatch.setenv("HF_TOKEN", "hf_test_token")
    harness = hub_harness({"FAKE_SPLASH_DL_FAIL": "gated"})
    started = queue(harness, MODEL)
    done = wait_for(harness, started["id"], "done")
    assert done["error"] is None


def test_a_network_failure_is_reported_and_can_be_retried(hub_harness):
    harness = hub_harness({"FAKE_SPLASH_DL_FAIL": "network_mid"})
    started = queue(harness, MODEL)
    failed = wait_for(harness, started["id"], "failed")
    assert failed["error"]["code"] == "installer_failed"
    assert failed["error"]["message"]
    # resume clears the error and retries (SPEC §9.4).
    response = harness.client.post(f"/api/admin/downloads/{started['id']}/resume")
    assert response.status_code == 200, response.text
    assert response.json()["error"] is None


def test_a_disk_full_failure_is_classified(hub_harness):
    harness = hub_harness({"FAKE_SPLASH_DL_FAIL": "disk_full"})
    started = queue(harness, MODEL)
    failed = wait_for(harness, started["id"], "failed")
    # ENOSPC ("No space left on device") must not fall through to installer_failed.
    assert failed["error"]["code"] == "disk_full"


def test_a_failed_download_raises_an_alert(hub_harness):
    harness = hub_harness({"FAKE_SPLASH_DL_FAIL": "network"})
    started = queue(harness, MODEL)
    wait_for(harness, started["id"], "failed")
    alerts = harness.client.get("/api/admin/alerts").json()["alerts"]
    assert any(a["id"] == f"download_failed:{started['id']}" for a in alerts)


def test_an_incompatible_model_is_refused_before_any_download(hub_harness):
    """D7: compatibility is checked before download, so nothing is fetched."""
    harness = hub_harness()
    response = harness.client.post("/api/admin/downloads", json={"id": MLX_8BIT})
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "incompatible"
    assert body["message"], "the reason from the compatibility check is shown"
    assert harness.client.get("/api/admin/downloads").json()["items"] == []


def test_a_model_without_a_usable_projector_needs_language_only(hub_harness):
    """SPEC §9.4 / D7: a repository with no usable projector is refused unless
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
    """SPEC §9.2 badge: "Compatible — text only"."""
    harness = hub_harness()
    response = harness.client.get(f"/api/admin/inspect?id={GGUF_NO_VISION}&refresh=1")
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
    assert response.json()["error"]["code"] == "download_exists"


def test_a_short_model_id_is_rejected(hub_harness):
    """D22: full Hugging Face IDs only, no short aliases."""
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
    """SPEC §9.4: downloads.json is the persisted queue, and an interrupted
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
    `running`; it is requeued instead (SPEC §9.4)."""
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
    """SPEC §9.4: the draft repo's config and weights are part of the download."""
    harness = hub_harness()
    started = queue(harness, MLX)
    done = wait_for(harness, started["id"], "done")
    repos = {f["repo_id"] for f in done["files"]}
    assert MLX in repos
    assert "incoai/Qwen3.6-35B-A3B-DFlash2" in repos, repos


def test_a_download_that_does_not_fit_on_disk_is_refused_up_front(hub_harness, monkeypatch):
    """SPEC §9.4 disk check: remaining bytes plus a 2 GB margin, before queuing."""
    import shutil

    harness = hub_harness()
    usage = shutil.disk_usage(harness.home)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage._replace(free=10**9))
    response = harness.client.post("/api/admin/downloads", json={"id": f"{GGUF}:UD-Q4_K_M"})
    assert response.status_code == 507, response.text
    assert response.json()["error"]["code"] == "disk_full"
    assert harness.client.get("/api/admin/downloads").json()["items"] == []

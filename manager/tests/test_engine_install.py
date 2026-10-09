"""A load that (re)installs: progress, confirmation and stale partials.

Splash's launcher prints `Fetching N file(s), X GB, from REPO@REV12; cached files
are reused.` (splash/install/hub.py:352) and then downloads with huggingface_hub
1.28, which writes a per-process `<etag>.<uuid8>.incomplete` partial that no later
run can continue (huggingface_hub/file_download.py:1949).
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any

import pytest

from splash_gui.engine import install as inst
from splash_gui.hubcache import blobs_dir, open_paths

from .fakeengine import MODEL, MODEL_27B, REPO, EngineHarness

GGUF_REPO = "unsloth/Qwen3.6-35B-A3B-GGUF"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_D = "d" * 40


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


# --- The Fetching line ---------------------------------------------------------------------


def test_fetching_line_is_parsed_as_splash_prints_it() -> None:
    line = (
        f"Fetching {3} file(s), {19_934_000_000 / 1e9:.2f} GB, "
        f"from mlx-community/Qwen3.8-27B-4bit@{'0123456789ab'}; cached files are reused."
    )
    fetch = inst.parse_fetching(line)
    assert fetch == inst.FetchLine(
        repo="mlx-community/Qwen3.8-27B-4bit",
        revision="0123456789ab",
        files=3,
        total_bytes=19_930_000_000,
    )
    # The engine log may carry a prefix; other lines are not Fetching lines.
    assert inst.parse_fetching("stdout: " + line) == fetch
    assert inst.parse_fetching("Selected Qwen3.6-35B-A3B-UD-Q4_K_M.gguf from x/y.") is None


def test_fetching_pattern_matches_the_engine_source() -> None:
    source = REPO / "splash" / "install" / "hub.py"
    if not source.is_file():
        pytest.skip("the read-only splash/ reference clone is not checked out")
    text = source.read_text()
    assert 'f"Fetching {len(fetch)} file(s), {sum(fetch) / 1e9:.2f} GB, "' in text
    assert 'f"from {self.name}@{self.revision[:12]}; cached files are reused."' in text


# --- InstallTracker ---------------------------------------------------------------------------


def test_tracker_counts_this_runs_partials_and_new_blobs_only(tmp_path: Path) -> None:
    blobs = blobs_dir(tmp_path, GGUF_REPO)
    write(blobs / DIGEST_A, 5_000)  # cached before the line: not part of the fetch
    stale = write(blobs / f"{DIGEST_B}.0badf00d.incomplete", 7_000)  # a killed run's
    clock = Clock()
    fetch = inst.FetchLine(GGUF_REPO, "0123456789ab", 2, 30_000)
    tracker = inst.InstallTracker(tmp_path, fetch, clock=clock)
    tracker.sample()
    assert tracker.done_bytes == 0, "neither a cached blob nor a stale partial is progress"

    live = write(blobs / f"{DIGEST_B}.1234abcd.incomplete", 4_000)
    clock.now += 1
    tracker.sample()
    assert tracker.done_bytes == 4_000
    assert tracker.speed_bps == pytest.approx(4_000)

    live.rename(blobs / DIGEST_B)  # finished: renamed to its blob
    write(blobs / f"{DIGEST_C}.5678abcd.incomplete", 6_000)
    clock.now += 1
    tracker.sample()
    assert tracker.done_bytes == 4_000 + 6_000
    view = tracker.view()
    assert (view.repo, view.revision, view.files) == (GGUF_REPO, "0123456789ab", 2)
    assert view.total_bytes == 30_000 and view.done_bytes == 10_000
    assert view.speed_bps is not None and view.eta_s is not None
    assert view.eta_s == pytest.approx(20_000 / view.speed_bps, abs=0.1)
    assert stale.exists(), "the tracker never deletes anything"


def test_tracker_leaves_out_what_the_downloader_is_fetching(tmp_path: Path) -> None:
    """Review finding: a Downloader download of the same repository inflated the
    engine's install progress."""
    blobs = blobs_dir(tmp_path, GGUF_REPO)
    clock = Clock()
    fetch = inst.FetchLine(GGUF_REPO, "0123456789ab", 1, 10_000)
    tracker = inst.InstallTracker(tmp_path, fetch, clock=clock, excluded=lambda: {DIGEST_B})
    write(blobs / f"{DIGEST_A}.1234abcd.incomplete", 3_000)  # this install's
    write(blobs / f"{DIGEST_B}.5678abcd.incomplete", 9_000)  # the Downloader's
    write(blobs / DIGEST_B, 1)  # ... and a Downloader blob finishing
    clock.now += 1
    tracker.sample()
    assert tracker.done_bytes == 3_000


def test_tracker_counts_a_blob_finished_just_before_the_first_scan(tmp_path: Path) -> None:
    blobs = blobs_dir(tmp_path, GGUF_REPO)
    old = write(blobs / DIGEST_A, 5_000)
    os.utime(old, (time.time() - 3600, time.time() - 3600))  # cached long ago
    write(blobs / DIGEST_B, 2_000)  # written as the line was read
    fetch = inst.FetchLine(GGUF_REPO, "0123456789ab", 2, 10_000)
    tracker = inst.InstallTracker(tmp_path, fetch, clock=Clock(), line_wall=time.time())
    tracker.sample()
    assert tracker.done_bytes == 2_000


def test_tracker_total_never_below_done(tmp_path: Path) -> None:
    """Splash prints two decimals of GB: small files print 0.00 GB."""
    fetch = inst.FetchLine(GGUF_REPO, "0123456789ab", 1, 0)
    tracker = inst.InstallTracker(tmp_path, fetch, clock=Clock())
    write(blobs_dir(tmp_path, GGUF_REPO) / f"{DIGEST_A}.1234abcd.incomplete", 3_000)
    tracker.sample()
    assert tracker.view().total_bytes == 3_000 and tracker.view().done_bytes == 3_000


# --- Stale partials ---------------------------------------------------------------------------


def test_remove_stale_partials_keeps_open_legacy_protected_and_other_repos(
    tmp_path: Path,
) -> None:
    blobs = blobs_dir(tmp_path, GGUF_REPO)
    stale = write(blobs / f"{DIGEST_A}.0badf00d.incomplete", 1_000)
    stale_sha1 = write(blobs / f"{DIGEST_D}.11112222.incomplete", 10)
    busy = write(blobs / f"{DIGEST_B}.12345678.incomplete", 2_000)
    legacy = write(blobs / f"{DIGEST_A}.incomplete", 3_000)
    protected = write(blobs / f"{DIGEST_C}.87654321.incomplete", 4_000)
    blob = write(blobs / DIGEST_B, 5_000)
    other = write(blobs_dir(tmp_path, "someone/else") / f"{DIGEST_A}.0badf00d.incomplete", 6)
    asked: list[list[Path]] = []

    def lsof(paths: list[Path]) -> set[str]:
        asked.append(paths)
        return {str(busy)}

    removed = inst.remove_stale_partials(
        tmp_path, [GGUF_REPO, GGUF_REPO], protected_digests={DIGEST_C}, lsof=lsof
    )
    assert sorted(r.path.name for r in removed) == sorted([stale.name, stale_sha1.name])
    assert sum(r.size for r in removed) == 1_010
    assert not stale.exists() and not stale_sha1.exists()
    assert busy.exists() and legacy.exists() and protected.exists() and blob.exists()
    assert other.exists(), "only the model's own repositories are cleaned"
    assert len(asked) == 1 and legacy not in asked[0] and protected not in asked[0]


def test_remove_stale_partials_without_lsof_keeps_recent_writes(tmp_path: Path) -> None:
    blobs = blobs_dir(tmp_path, GGUF_REPO)
    old = write(blobs / f"{DIGEST_A}.0badf00d.incomplete", 10)
    recent = write(blobs / f"{DIGEST_B}.12345678.incomplete", 10)
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    removed = inst.remove_stale_partials(
        tmp_path, [GGUF_REPO], protected_digests=set(), lsof=lambda paths: None
    )
    assert [r.path for r in removed] == [old]
    assert recent.exists()


def test_open_paths_sees_an_open_file(tmp_path: Path) -> None:
    if not any(Path(d, "lsof").exists() for d in ("/usr/sbin", "/usr/bin")):
        pytest.skip("lsof is not installed")
    held = write(tmp_path / f"{DIGEST_A}.12345678.incomplete", 10)
    closed = write(tmp_path / f"{DIGEST_B}.12345678.incomplete", 10)
    with held.open("ab"):
        found = open_paths([held, closed])
    assert found == {str(held)}


# --- The supervisor, against the fake engine --------------------------------------------------


def engine_until(h: EngineHarness, predicate: Any, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    view = h.engine()
    while not predicate(view):
        if time.monotonic() > deadline:
            raise AssertionError(f"engine never reached the condition: {view}")
        time.sleep(0.05)
        view = h.engine()
    return view


def test_load_that_installs_shows_progress_and_asks_before_stopping(
    harness_factory: Any,
) -> None:
    # Slow enough to watch: a 6 MB GGUF and its projector at 3 MB/s.
    h: EngineHarness = harness_factory(
        installed=(),
        env={"FAKE_SPLASH_DL_SHARD_BYTES": "6M", "FAKE_SPLASH_DL_BPS": "3M"},
    )
    h.sup.install_sample_s = 0.1
    h.set_installed(MODEL, MODEL_27B)
    published: list[dict[str, Any]] = []
    h.state.events.listeners.append(
        lambda name, data: published.append(data) if name == "engine.state" else None
    )
    blobs = blobs_dir(h.home / "models", GGUF_REPO)
    stale = write(blobs / f"{DIGEST_A}.0badf00d.incomplete", 1_000)
    legacy = write(blobs / f"{DIGEST_B}.incomplete", 1_000)
    needed = write(blobs / f"{DIGEST_C}.87654321.incomplete", 1_000)
    h.state.downloads.active_digests = lambda exclude=None: {DIGEST_C}

    assert h.client.post("/api/admin/engine/load", json={"model": MODEL}).status_code == 202
    view = engine_until(h, lambda v: v["install"] is not None)
    assert view["state"] == "starting" and view["phase"] == "installing"
    install = view["install"]
    assert install["repo"] == GGUF_REPO and len(install["revision"]) == 12
    assert install["files"] == 2 and install["total_bytes"] >= 6_000_000
    assert set(install) == {
        "repo",
        "revision",
        "files",
        "total_bytes",
        "done_bytes",
        "speed_bps",
        "eta_s",
    }
    # Bug 5: what a killed run left is gone at start; the rest stays.
    assert not stale.exists()
    assert legacy.exists() and needed.exists()

    later = engine_until(
        h, lambda v: v["install"] is not None and v["install"]["done_bytes"] > 1_000_000
    )["install"]
    assert later["speed_bps"] and later["speed_bps"] > 0 and later["eta_s"] is not None
    assert any((e.get("install") or {}).get("done_bytes") for e in published), (
        "progress reaches SSE clients as engine.state events"
    )

    # A load of another model and a restart need force.
    for path, body in (
        ("/api/admin/engine/load", {"model": MODEL_27B}),
        ("/api/admin/engine/restart", None),
        ("/api/admin/engine/restart", {"force": False}),
    ):
        response = h.client.post(path, json=body)
        assert response.status_code == 409, (path, body, response.text)
        error = response.json()["error"]
        assert error["code"] == "install_in_progress"
        assert "restarts the file in progress" in error["message"]
    # The model being installed: nothing would stop, so no 409; `wait` waits for it.
    same = h.client.post("/api/admin/engine/load", json={"model": MODEL})
    assert same.status_code == 202, same.text
    assert same.json()["state"] == "starting" and same.json()["install"] is not None
    assert h.engine()["state"] == "starting", "a refused load leaves the install running"

    waited = h.client.post("/api/admin/engine/load", json={"model": MODEL, "wait": True})
    assert waited.status_code == 202, waited.text
    assert waited.json()["state"] == "ready" and waited.json()["install"] is None


def test_force_restart_and_a_killed_install_leave_no_partials(harness_factory: Any) -> None:
    h: EngineHarness = harness_factory(
        installed=(),
        env={"FAKE_SPLASH_DL_SHARD_BYTES": "64M", "FAKE_SPLASH_DL_BPS": "4M"},
    )
    h.sup.install_sample_s = 0.1
    h.set_installed(MODEL)
    blobs = blobs_dir(h.home / "models", GGUF_REPO)
    assert h.client.post("/api/admin/engine/load", json={"model": MODEL}).status_code == 202
    engine_until(h, lambda v: v["install"] is not None and v["install"]["done_bytes"] > 0)
    response = h.client.post("/api/admin/engine/restart", json={"force": True})
    assert response.status_code == 202, response.text
    view = engine_until(h, lambda v: v["install"] is not None and v["install"]["done_bytes"] > 0)
    # Killed outright (what a stop that reaches SIGKILL does): huggingface_hub never
    # gets to delete its partial, so the session end must.
    pid = view["pid"]
    assert pid and list(blobs.glob("*.*.incomplete"))
    os.killpg(pid, signal.SIGKILL)
    after = h.wait_state("failed", "stopped", "crashed", timeout=20)
    assert after["install"] is None
    leftovers = [p.name for p in blobs.glob("*.*.incomplete")]
    assert leftovers == [], "a killed install leaves no per-process partials behind"


def test_downloads_active_digests_cover_unfinished_downloads(harness_factory: Any) -> None:
    h: EngineHarness = harness_factory(installed=())
    downloads = h.state.downloads
    from splash_gui.schemas import DownloadItem

    for key, state in (("run", "running"), ("pause", "paused"), ("done", "done")):
        downloads.items[key] = DownloadItem.model_validate(
            {"id": key, "model": MODEL, "state": state, "created_at": "2026-10-04T00:00:00Z"}
        )
        downloads.blobs[key] = {f"{GGUF_REPO}/{key}.gguf": key * 8}
    assert downloads.active_digests() == {"run" * 8, "pause" * 8}
    assert downloads.shared_digests("run") == {"pause" * 8}


def test_auto_load_during_an_install_does_not_stop_it(harness_factory: Any) -> None:
    h: EngineHarness = harness_factory(
        installed=(),
        env={"FAKE_SPLASH_DL_SHARD_BYTES": "64M", "FAKE_SPLASH_DL_BPS": "4M"},
    )
    h.set_installed(MODEL, MODEL_27B)
    assert h.client.post("/api/admin/engine/load", json={"model": MODEL}).status_code == 202
    engine_until(h, lambda v: v["install"] is not None)
    response = h.client.post(
        "/v1/chat/completions",
        json={"model": MODEL_27B, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 503, response.text
    assert response.headers.get("retry-after") == "30"
    view = h.engine()
    assert view["model"] == MODEL and view["state"] == "starting"
    h.client.post("/api/admin/engine/stop")

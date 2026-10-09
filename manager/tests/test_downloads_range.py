"""Byte-level download resume against the fake Hub and its CDN.

The manager fetches large LFS/Xet files itself with HTTP Range requests into the Hub
cache, then runs the fake `install/models.py prepare`, which skips blobs that exist.
The fake Hub answers `HEAD …/resolve/…` with the real Hub's 302 + `X-Linked-Etag`
shape and its CDN (another port, so another host) serves `206` ranges, so every
request the manager makes is recorded and checked here. A large file is fetched in
`RANGE_SEGMENTS` parallel ranges; the one-stream fallback, the old one-stream
sidecar and a hard kill mid-segment are covered too. The size threshold is lowered to
3 MiB so the 4 MiB fake GGUF takes the Range path; the projector and the draft stay
with the installer.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest

from splash_gui.downloads import ranged

from .conftest import FAKE_SPLASH, fake_hub_module
from .fakeengine import MODEL

REPO_ID = "unsloth/Qwen3.6-35B-A3B-GGUF"
WEIGHTS = "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
KEY = f"{REPO_ID}/{WEIGHTS}"
SHARD = 4 << 20
MANAGER_DIR = Path(__file__).resolve().parents[1]
TOKEN = "hf_rangeTestToken0123456789"


@pytest.fixture
def setup(harness_factory, fake_hub_server, monkeypatch):
    """A manager on the fake engine whose Hub is the fake Hub; the GGUF is 4 MiB and
    anything of 3 MiB or more takes the Range path."""
    monkeypatch.setattr(ranged, "RANGE_MIN_BYTES", 3 << 20)
    monkeypatch.setattr(ranged, "RETRY_DELAY_S", 0.05)
    env = {"HF_ENDPOINT": fake_hub_server.url, "FAKE_SPLASH_DL_SHARD_BYTES": "4M"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def make(cdn_bps: int = 512 << 10) -> Any:
        fake_hub_server.cdn_bps = cdn_bps
        harness = harness_factory(installed=(), env=env)
        harness.patch_settings({"global": {"hf": {"endpoint": fake_hub_server.url}}})
        return harness

    return make


def remote_file(name: str = WEIGHTS, repo: str = REPO_ID) -> Any:
    _, files, _ = fake_hub_module().repository(repo, None)
    return files[name]


def blobs(harness: Any) -> Path:
    models = Path(harness.state.settings.models_dir())
    return models / ("models--" + REPO_ID.replace("/", "--")) / "blobs"


def queue(harness: Any, model: str = MODEL) -> str:
    response = harness.client.post("/api/admin/downloads", json={"id": model})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def item(harness: Any, dl: str) -> dict[str, Any]:
    rows = harness.client.get("/api/admin/downloads").json()["items"]
    return dict(next(r for r in rows if r["id"] == dl))


def weights(row: dict[str, Any]) -> dict[str, Any]:
    return dict(next(f for f in row["files"] if f["name"] == WEIGHTS))


def wait(harness: Any, dl: str, test: Any, what: str, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    row = item(harness, dl)
    while not test(row):
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}: {row}")
        time.sleep(0.05)
        row = item(harness, dl)
    return row


def partway(harness: Any, dl: str, at_least: int = 1 << 20) -> dict[str, Any]:
    return wait(harness, dl, lambda r: weights(r)["done_bytes"] >= at_least, f"{at_least} bytes")


def done(harness: Any, dl: str) -> dict[str, Any]:
    return wait(harness, dl, lambda r: r["state"] in ("done", "failed"), "the end")


def cdn_gets(hub: Any) -> list[dict[str, Any]]:
    return [r for r in hub.requests_to("cdn") if r["method"] == "GET"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def range_files(harness: Any) -> list[str]:
    return sorted(p.name for p in blobs(harness).glob("*.splashgui.*"))


def segments_on_disk(sidecar: Path) -> list[dict[str, int]]:
    """The segments a version 2 sidecar records: `start`, `end`, `verified_end`."""
    segments: list[dict[str, int]] = json.loads(sidecar.read_text())["segments"]
    return segments


def done_on_disk(sidecar: Path) -> int:
    """The verified bytes a version 2 sidecar records; -1 while it cannot be read."""
    try:
        return sum(s["verified_end"] - s["start"] for s in segments_on_disk(sidecar))
    except (OSError, ValueError, KeyError):
        return -1


def xet_of(hub: Any, sha: str) -> str:
    """The CDN's name for a blob: the Xet hash the fake Hub answered its HEAD with."""
    return str(next(xet for xet, remote in hub.cdn_files.items() if remote.blob == sha))


def gets_of(hub: Any, sha: str) -> list[dict[str, Any]]:
    """The CDN's GET requests for one blob, in the order they arrived."""
    suffix = "/" + xet_of(hub, sha)
    return [r for r in cdn_gets(hub) if r["path"].endswith(suffix)]


def span(get: dict[str, Any]) -> tuple[int, int]:
    """The first and the last byte a GET's Range asked for."""
    first, _, last = get["headers"]["range"].removeprefix("bytes=").partition("-")
    return int(first), int(last)


def hub_remote(hub: Any) -> ranged.Remote:
    """The weights as the Hub describes them, read the way the Downloader reads them."""

    async def resolve() -> ranged.Remote:
        downloader = ranged.RangeDownloader(Path(), hub.url, None)
        async with downloader.client() as client:
            return await downloader.resolve(client, REPO_ID, None, WEIGHTS)

    return asyncio.run(resolve())


def test_pause_keeps_every_segment_and_resume_continues_each_from_its_verified_end(
    setup, fake_hub_server
):
    harness = setup()
    dl = queue(harness)
    queued = item(harness, dl)
    assert weights(queued)["resumable"] is True
    assert not any(f["resumable"] for f in queued["files"] if f["name"] != WEIGHTS)
    partway(harness, dl)

    paused = harness.client.post(f"/api/admin/downloads/{dl}/pause").json()
    assert paused["state"] == "paused"
    sha = remote_file().blob
    partial = blobs(harness) / f"{sha}.splashgui.incomplete"
    sidecar = blobs(harness) / f"{sha}.splashgui.json"
    segments = segments_on_disk(sidecar)
    at_pause = done_on_disk(sidecar)
    assert len(segments) == ranged.RANGE_SEGMENTS
    assert 0 < at_pause < SHARD
    assert weights(paused)["done_bytes"] == at_pause, "Pause reports the verified bytes"
    assert partial.stat().st_size == SHARD, "the partial is preallocated to the file's size"
    time.sleep(1.0)
    assert segments_on_disk(sidecar) == segments, "nothing writes while paused"
    assert weights(item(harness, dl))["done_bytes"] == at_pause, "progress is the verified bytes"
    before = len(gets_of(fake_hub_server, sha))

    fake_hub_server.cdn_bps = 0
    harness.client.post(f"/api/admin/downloads/{dl}/resume")
    finished = done(harness, dl)
    assert finished["state"] == "done", finished

    # Each unfinished segment resumes at its own verified end; no verified byte is fetched again.
    after = gets_of(fake_hub_server, sha)[before:]
    unfinished = [s for s in segments if s["verified_end"] < s["end"]]
    assert sorted(span(g) for g in after) == sorted(
        (s["verified_end"], s["end"] - 1) for s in unfinished
    )
    assert all(g["status"] == 206 for g in after)
    assert f"resuming in {ranged.RANGE_SEGMENTS} ranges from {at_pause} bytes." in "\n".join(
        finished["log_tail"]
    )
    blob = blobs(harness) / sha
    assert blob.read_bytes() == remote_file().read(0, SHARD), "the final bytes are the file's"
    assert sha256(blob) == sha
    assert range_files(harness) == [], "partial and sidecar are gone after the rename"
    # Splash's installer found the blob: only the projector was left for it.
    fetching = [line for line in finished["log_tail"] if line.startswith("Fetching")]
    assert any(f"1 file(s), 0.00 GB, from {REPO_ID}@" in line for line in fetching), fetching
    pointer = blobs(harness).parent / "snapshots"
    links = [p for p in pointer.rglob(WEIGHTS) if p.is_symlink()]
    assert links and links[0].resolve() == blob.resolve()


def test_a_large_file_is_fetched_in_parallel_range_segments(setup, fake_hub_server):
    harness = setup()
    dl = queue(harness)
    finished = done(harness, dl)
    assert finished["state"] == "done", finished
    sha = remote_file().blob
    gets = gets_of(fake_hub_server, sha)
    assert len(gets) == ranged.RANGE_SEGMENTS, [g["headers"] for g in gets]
    assert all(g["status"] == 206 for g in gets)
    spans = sorted(span(g) for g in gets)
    assert spans[0][0] == 0 and spans[-1][1] == SHARD - 1, "the segments cover the file"
    assert all(a[1] + 1 == b[0] for a, b in pairwise(spans)), "and not twice"
    ends = [g["ended"] for g in gets]
    assert all(end is not None for end in ends)
    assert max(g["started"] for g in gets) < min(ends), "the streams ran at the same time"
    assert sha256(blobs(harness) / sha) == sha
    assert range_files(harness) == []
    log = "\n".join(finished["log_tail"])
    assert f"downloading in {ranged.RANGE_SEGMENTS} ranges." in log
    assert f"{WEIGHTS}: checking the sha256." in log


def test_a_server_that_ignores_range_is_fetched_as_one_stream(setup, fake_hub_server):
    fake_hub_server.ignore_range = True
    try:
        harness = setup(cdn_bps=0)
        dl = queue(harness)
        finished = done(harness, dl)
    finally:
        fake_hub_server.ignore_range = False
    assert finished["state"] == "done", finished
    sha = remote_file().blob
    gets = gets_of(fake_hub_server, sha)
    ranged_gets = [g for g in gets if "range" in g["headers"]]
    plain = [g for g in gets if "range" not in g["headers"]]
    assert ranged_gets, "the segments asked for their ranges first"
    assert all(g["status"] == 200 for g in ranged_gets), "and got the whole file back"
    assert len(plain) == 1 and gets[-1] is plain[0], "then one stream from byte 0"
    assert plain[0]["status"] == 200
    log = "\n".join(finished["log_tail"])
    assert "the server ignored Range; continuing as one stream from byte 0." in log
    assert sha256(blobs(harness) / sha) == sha
    assert range_files(harness) == []


def test_a_server_that_caps_each_answer_is_asked_again_for_the_rest(setup, fake_hub_server):
    """Ranges honoured but cut short (a proxy's limit): the segments keep asking, and the
    file is never handed to one stream."""
    fake_hub_server.max_range_bytes = 256 << 10
    try:
        harness = setup(cdn_bps=0)
        dl = queue(harness)
        finished = done(harness, dl)
    finally:
        fake_hub_server.max_range_bytes = 0
    assert finished["state"] == "done", finished
    sha = remote_file().blob
    gets = gets_of(fake_hub_server, sha)
    assert all(g["status"] == 206 and "range" in g["headers"] for g in gets)
    # Each segment's next request starts where its last answer ended: every 256 KiB.
    piece, quarter = 256 << 10, SHARD // ranged.RANGE_SEGMENTS
    expected = sorted(
        segment * quarter + k * piece
        for segment in range(ranged.RANGE_SEGMENTS)
        for k in range(quarter // piece)
    )
    assert sorted(span(g)[0] for g in gets) == expected, "four segments in 256 KiB pieces"
    assert sha256(blobs(harness) / sha) == sha
    assert range_files(harness) == []


def test_a_one_stream_sidecar_from_before_segments_resumes_as_one_stream(setup, fake_hub_server):
    harness = setup()
    remote = hub_remote(fake_hub_server)
    sha, keep = remote.etag, 1_500_000
    blobs(harness).mkdir(parents=True, exist_ok=True)
    (blobs(harness) / f"{sha}.splashgui.incomplete").write_bytes(remote_file().read(0, keep))
    # The sidecar as the one-stream downloader wrote it before parallel streams: no version, no
    # segments.
    (blobs(harness) / f"{sha}.splashgui.json").write_text(
        json.dumps(
            {
                "repo": REPO_ID,
                "path": WEIGHTS,
                "commit": remote.commit,
                "etag": sha,
                "size": remote.size,
                "xet_hash": remote.xet_hash,
                "validator": None,
            }
        )
    )
    dl = queue(harness)
    finished = done(harness, dl)
    assert finished["state"] == "done", finished
    gets = gets_of(fake_hub_server, sha)
    assert [(g["headers"].get("range"), g["status"]) for g in gets] == [(f"bytes={keep}-", 206)]
    assert f"resuming at byte {keep} (Range: bytes={keep}-) -> 206" in "\n".join(
        finished["log_tail"]
    )
    assert sha256(blobs(harness) / sha) == sha
    assert range_files(harness) == []


def test_a_hash_mismatch_deletes_the_partial_and_falls_back_to_prepare(setup, fake_hub_server):
    harness = setup()
    dl = queue(harness)
    partway(harness, dl)
    harness.client.post(f"/api/admin/downloads/{dl}/pause")
    sha = remote_file().blob
    partial = blobs(harness) / f"{sha}.splashgui.incomplete"
    data = bytearray(partial.read_bytes())
    data[100] ^= 0xFF
    partial.write_bytes(bytes(data))

    fake_hub_server.cdn_bps = 0
    harness.client.post(f"/api/admin/downloads/{dl}/resume")
    finished = done(harness, dl)
    assert finished["state"] == "done", finished
    log = "\n".join(finished["log_tail"])
    assert "did not match the Hub's sha256; the partial file was deleted" in log
    assert "Splash's installer downloads it instead" in log
    assert weights(finished)["resumable"] is False
    assert range_files(harness) == []
    assert sha256(blobs(harness) / sha) == sha, "the installer fetched a good copy"
    assert any(
        line.startswith("Fetching 2 file(s)") and REPO_ID in line for line in finished["log_tail"]
    ), "the installer downloaded the weights as well as the projector"


def test_a_full_disk_while_writing_a_range_fails_the_job_and_is_not_handed_to_the_installer(
    setup, fake_hub_server, monkeypatch
):
    """Only a problem with the Hub, the stream or the bytes falls back to the
    installer. An OS error while writing the partial is not one of them: the installer
    writes to the same disk, so the job fails with that error (ENOSPC is `disk_full`)."""
    harness = setup()

    def full_disk(self: Any, offset: int, chunk: bytes) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(ranged._Journal, "write", full_disk)
    dl = queue(harness)
    failed = done(harness, dl)
    assert failed["state"] == "failed", failed
    assert failed["error"]["code"] == "disk_full", failed["error"]
    log = "\n".join(failed["log_tail"])
    assert "Splash's installer downloads it instead" not in log, log
    assert not any(line.startswith("Fetching") for line in failed["log_tail"]), (
        "the installer never ran"
    )
    assert weights(failed)["resumable"] is True


def test_a_changed_remote_file_restarts_from_zero(setup, fake_hub_server, monkeypatch):
    harness = setup()
    dl = queue(harness)
    partway(harness, dl)
    harness.client.post(f"/api/admin/downloads/{dl}/pause")
    old = remote_file().blob
    assert done_on_disk(blobs(harness) / f"{old}.splashgui.json") > 0

    # A new commit on main changes the weights file (and only it).
    monkeypatch.setenv("FAKE_SPLASH_DL_COMMIT_SALT", "v2")
    new = remote_file().blob
    assert new != old
    fake_hub_server.cdn_bps = 0
    harness.client.post(f"/api/admin/downloads/{dl}/resume")
    finished = done(harness, dl)
    assert finished["state"] == "done", finished

    fresh = gets_of(fake_hub_server, new)
    starts = sorted(span(g)[0] for g in fresh)
    assert starts == [i * SHARD // ranged.RANGE_SEGMENTS for i in range(ranged.RANGE_SEGMENTS)], (
        "the new version starts at byte 0 in every segment"
    )
    assert all(g["status"] == 206 for g in fresh)
    assert not (blobs(harness) / f"{old}.splashgui.incomplete").exists()
    assert not (blobs(harness) / f"{old}.splashgui.json").exists()
    assert sha256(blobs(harness) / new) == new
    assert harness.state.downloads.blobs[dl][KEY] == new
    assert "changed on the Hub; downloading the new version from 0" in "\n".join(
        finished["log_tail"]
    )


def test_a_file_under_the_threshold_uses_splashs_installer(harness_factory, fake_hub_server):
    """The real threshold (1 GB): every fake file is far below it."""
    assert ranged.RANGE_MIN_BYTES == 1_000_000_000
    harness = harness_factory(installed=(), env={"HF_ENDPOINT": fake_hub_server.url})
    harness.patch_settings({"global": {"hf": {"endpoint": fake_hub_server.url}}})
    dl = queue(harness)
    finished = done(harness, dl)
    assert finished["state"] == "done", finished
    assert not any(f["resumable"] for f in finished["files"])
    assert fake_hub_server.requests_to("cdn") == []
    assert not [r for r in fake_hub_server.requests_to("hub") if r["method"] == "HEAD"]


def test_a_huggingface_hub_lock_holder_is_waited_for(setup, fake_hub_server):
    """Splash's installer (huggingface_hub) holds `.locks/<repo>/<etag>.lock` while it
    writes a blob. The Range download waits, then finds the blob and fetches nothing."""
    harness = setup()
    sha = remote_file().blob
    models = harness.state.settings.models_dir()
    lock = models / ".locks" / ("models--" + REPO_ID.replace("/", "--")) / f"{sha}.lock"
    lock.parent.mkdir(parents=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o664)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        dl = queue(harness)
        wait(
            harness,
            dl,
            lambda r: any("waiting for it" in line for line in r["log_tail"]),
            "the lock wait",
        )
        # The other writer finishes the blob, then lets go.
        blobs(harness).mkdir(parents=True, exist_ok=True)
        (blobs(harness) / sha).write_bytes(remote_file().read(0, SHARD))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    finished = done(harness, dl)
    assert finished["state"] == "done", finished
    assert "is already in the models folder" in "\n".join(finished["log_tail"])
    assert cdn_gets(fake_hub_server) == [], "nothing was downloaded twice"
    assert range_files(harness) == []


def test_a_lock_held_too_long_falls_back_to_prepare(setup, fake_hub_server, monkeypatch):
    monkeypatch.setattr(ranged, "LOCK_TIMEOUT_S", 0.5)
    harness = setup(cdn_bps=0)
    sha = remote_file().blob
    models = harness.state.settings.models_dir()
    lock = models / ".locks" / ("models--" + REPO_ID.replace("/", "--")) / f"{sha}.lock"
    lock.parent.mkdir(parents=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o664)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        dl = queue(harness)
        finished = done(harness, dl)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    # The fake installer takes no hub lock, so it simply downloads the file.
    assert finished["state"] == "done", finished
    assert "held the file's lock" in "\n".join(finished["log_tail"])
    assert cdn_gets(fake_hub_server) == []
    assert sha256(blobs(harness) / sha) == sha


def test_the_token_goes_to_the_hub_and_never_to_the_cdn(
    setup, fake_hub_server, monkeypatch, caplog
):
    monkeypatch.setenv("HF_TOKEN", TOKEN)
    harness = setup(cdn_bps=0)
    with caplog.at_level(logging.INFO, logger="splash_gui.downloads"):
        dl = queue(harness)
        finished = done(harness, dl)
    assert finished["state"] == "done", finished
    heads = [r for r in fake_hub_server.requests_to("hub") if r["method"] == "HEAD"]
    assert heads and all(r["headers"].get("authorization") == f"Bearer {TOKEN}" for r in heads)
    gets = cdn_gets(fake_hub_server)
    assert gets and all("authorization" not in r["headers"] for r in gets)
    assert all("Signature=" in r["query"] for r in gets), "sanity: the CDN URL is signed"
    logged = caplog.text + "\n".join(finished["log_tail"])
    assert "range GET" in caplog.text
    assert TOKEN not in logged
    assert "Signature=" not in logged and "Expires=" not in logged


def test_cancel_deletes_the_partial_and_its_sidecar(setup):
    harness = setup()
    dl = queue(harness)
    partway(harness, dl)
    assert range_files(harness)
    assert harness.client.delete(f"/api/admin/downloads/{dl}").status_code == 204
    assert item(harness, dl)["state"] == "cancelled"
    assert range_files(harness) == []


def test_the_stale_partial_cleanup_knows_the_range_partial(tmp_path):
    from splash_gui.engine.install import remove_stale_partials

    directory = tmp_path / ("models--" + REPO_ID.replace("/", "--")) / "blobs"
    directory.mkdir(parents=True)
    ours, claimed, orphan = "a" * 64, "b" * 64, "c" * 64
    for digest in (ours, claimed):
        (directory / f"{digest}.splashgui.incomplete").write_bytes(b"x")
        (directory / f"{digest}.splashgui.json").write_text("{}")
    (directory / f"{orphan}.splashgui.json").write_text("{}")
    removed = remove_stale_partials(
        tmp_path, [REPO_ID], protected_digests={claimed}, lsof=lambda _paths: set()
    )
    assert [r.path.name for r in removed] == [f"{ours}.splashgui.incomplete"]
    assert sorted(p.name for p in directory.iterdir()) == [
        f"{claimed}.splashgui.incomplete",
        f"{claimed}.splashgui.json",
    ]


def test_a_segment_answer_starts_at_its_verified_end_and_stays_inside_the_segment():
    segment = ranged.Segment(start=0, end=100, verified_end=40)
    assert ranged._check_range("bytes 40-99/1000", segment, 1000) == 99
    assert ranged._check_range("bytes 40-99/*", segment, 1000) == 99
    assert ranged._check_range("bytes 40-60/1000", segment, 1000) == 60, "a capped answer"
    for header in (
        "bytes 0-99/1000",
        "bytes 41-99/1000",
        "bytes 40-100/1000",
        "bytes 40-39/1000",
        "bytes 40-99/999",
        "bytes 40-99",
        None,
        "nonsense",
    ):
        with pytest.raises(ranged._Mismatch):
            ranged._check_range(header, segment, 1000)


def test_a_segmented_partial_reports_its_verified_bytes_not_its_size(tmp_path):
    sha = "ab" * 32
    partial = tmp_path / f"{sha}.splashgui.incomplete"
    partial.write_bytes(bytes(1000))  # preallocated: the full size, not all of it verified
    sidecar = tmp_path / f"{sha}.splashgui.json"
    sidecar.write_text(
        json.dumps(
            {
                "repo": REPO_ID,
                "path": WEIGHTS,
                "commit": "c",
                "etag": sha,
                "size": 1000,
                "xet_hash": None,
                "validator": None,
                "version": 2,
                "segments": [
                    {"start": 0, "end": 500, "verified_end": 120},
                    {"start": 500, "end": 1000, "verified_end": 700},
                ],
            }
        )
    )
    assert ranged.partial_progress(partial) == 120 + 200
    sidecar.unlink()
    assert ranged.partial_progress(partial) == 0, "without its sidecar nothing claims it"


# A hard kill of the whole manager mid-file --------------------------------------------

_AVOID = {8123, 9999, *range(8000, 8011)}


def _port() -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in _AVOID:
            return port


# The manager's own entry point, with the threshold lowered as in the tests above.
_MAIN = (
    "import sys; from splash_gui.downloads import ranged; ranged.RANGE_MIN_BYTES = 3 << 20; "
    "from splash_gui.manager import main; sys.argv[0] = 'splash-gui-manager'; "
    "raise SystemExit(main())"
)


class Manager:
    def __init__(self, home: Path, hub: Any, log: Path) -> None:
        self.home, self.hub, self.log = home, hub, log
        self.port = _port()
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        settings = self.home / "settings.json"
        if not settings.exists():
            settings.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "global": {
                            "server": {"host": "127.0.0.1", "port": self.port},
                            "wizard": {"completed": True},
                            "hf": {"endpoint": self.hub.url},
                        },
                    }
                )
            )
        env = {
            **os.environ,
            "SPLASH_GUI_HOME": str(self.home),
            "SPLASH_GUI_SECRETS": "file",
            "SPLASH_GUI_REAL_SPLASH": str(FAKE_SPLASH / "pkg" / "bin" / "splash"),
            "SPLASH_GUI_FAKE_DATA": str(self.home / "fake-data"),
            "FAKE_SPLASH_PYTHON": sys.executable,
            "FAKE_SPLASH_DL_SHARD_BYTES": "4M",
            "HF_ENDPOINT": self.hub.url,
            "HF_HUB_CACHE": str(self.home / "models"),
        }
        with self.log.open("ab") as out:
            self.process = subprocess.Popen(
                [sys.executable, "-c", _MAIN, "--port", str(self.port)],
                cwd=MANAGER_DIR,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 60
        while True:
            assert self.process.poll() is None, self.log.read_text()
            try:
                if httpx.get(f"{self.base}/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, "the manager did not start"
            time.sleep(0.2)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def api(self, method: str, path: str, body: Any = None) -> Any:
        token = (self.home / "run" / "cli.token").read_text().strip()
        response = httpx.request(
            method,
            f"{self.base}/api/admin{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        assert response.status_code < 300, response.text
        return response.json() if response.content else None

    def kill(self) -> None:
        assert self.process
        os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()


@pytest.fixture
def manager_process(tmp_path: Path, fake_hub_server: Any, monkeypatch) -> Iterator[Manager]:
    monkeypatch.setenv("FAKE_SPLASH_DL_SHARD_BYTES", "4M")
    manager = Manager(tmp_path / "home", fake_hub_server, tmp_path / "manager.log")
    try:
        yield manager
    finally:
        manager.stop()


def test_a_hard_killed_manager_resumes_every_segment_from_its_verified_end(manager_process):
    manager, hub = manager_process, manager_process.hub
    hub.cdn_bps = 512 << 10
    manager.start()
    dl = manager.api("POST", "/downloads", {"id": MODEL})["id"]
    sha = remote_file().blob
    folder = manager.home / "models" / ("models--" + REPO_ID.replace("/", "--")) / "blobs"
    partial = folder / f"{sha}.splashgui.incomplete"
    sidecar = folder / f"{sha}.splashgui.json"
    deadline = time.monotonic() + 60
    while done_on_disk(sidecar) < 1 << 20:
        assert time.monotonic() < deadline, "the Range download never got going"
        time.sleep(0.05)
    manager.kill()
    # The sidecar is final now: a restart may keep each segment's verified bytes, no more.
    unfinished = [s for s in segments_on_disk(sidecar) if s["verified_end"] < s["end"]]
    at_kill = done_on_disk(sidecar)
    assert 0 < at_kill < SHARD
    before = len(gets_of(hub, sha))

    hub.cdn_bps = 0
    manager.start()  # the queue comes back `queued` and runs again by itself
    deadline = time.monotonic() + 60
    while True:
        row = next(r for r in manager.api("GET", "/downloads")["items"] if r["id"] == dl)
        if row["state"] in ("done", "failed"):
            break
        assert time.monotonic() < deadline, row
        time.sleep(0.1)
    assert row["state"] == "done", row
    # Each unfinished segment continues at its verified end, and nothing below it is refetched.
    after = gets_of(hub, sha)[before:]
    assert sorted(span(g) for g in after) == sorted(
        (s["verified_end"], s["end"] - 1) for s in unfinished
    )
    assert all(g["status"] == 206 for g in after)
    assert sha256(folder / sha) == sha
    assert not partial.exists() and not sidecar.exists()
    assert "range GET" in (manager.home / "logs" / "manager.log").read_text()

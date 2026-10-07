"""Byte-level download resume (SPEC §9.4, D61) against the fake Hub and its CDN.

The manager fetches large LFS/Xet files itself with HTTP Range requests into the Hub
cache, then runs the fake `install/models.py prepare`, which skips blobs that exist.
The fake Hub answers `HEAD …/resolve/…` with the real Hub's 302 + `X-Linked-Etag`
shape and its CDN (another port, so another host) serves `206` ranges, so every
request the manager makes is recorded and checked here. The size threshold is
lowered to 3 MiB so the 4 MiB fake GGUF takes the Range path; the projector and the
draft stay with the installer.
"""

from __future__ import annotations

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


def test_pause_keeps_the_partial_and_resume_continues_from_its_byte_offset(setup, fake_hub_server):
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
    at_pause = partial.stat().st_size
    assert 0 < at_pause < SHARD
    assert (blobs(harness) / f"{sha}.splashgui.json").is_file(), "the sidecar stays too"
    time.sleep(1.0)
    assert partial.stat().st_size == at_pause, "nothing writes while paused"
    assert weights(item(harness, dl))["done_bytes"] == at_pause, "progress is the partial"
    before = len(cdn_gets(fake_hub_server))

    fake_hub_server.cdn_bps = 0
    harness.client.post(f"/api/admin/downloads/{dl}/resume")
    finished = done(harness, dl)
    assert finished["state"] == "done", finished

    after = cdn_gets(fake_hub_server)[before:]
    assert after, "the resume fetched the rest"
    assert after[0]["headers"].get("range") == f"bytes={at_pause}-"
    assert after[0]["status"] == 206
    assert f"resuming at byte {at_pause} (Range: bytes={at_pause}-) -> 206" in "\n".join(
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


def test_a_changed_remote_file_restarts_from_zero(setup, fake_hub_server, monkeypatch):
    harness = setup()
    dl = queue(harness)
    partway(harness, dl)
    harness.client.post(f"/api/admin/downloads/{dl}/pause")
    old = remote_file().blob
    assert (blobs(harness) / f"{old}.splashgui.incomplete").stat().st_size > 0

    # A new commit on main changes the weights file (and only it).
    monkeypatch.setenv("FAKE_SPLASH_DL_COMMIT_SALT", "v2")
    new = remote_file().blob
    assert new != old
    fake_hub_server.cdn_bps = 0
    before = len(cdn_gets(fake_hub_server))
    harness.client.post(f"/api/admin/downloads/{dl}/resume")
    finished = done(harness, dl)
    assert finished["state"] == "done", finished

    after = cdn_gets(fake_hub_server)[before:]
    assert after and "range" not in after[0]["headers"], "the new version starts at byte 0"
    assert after[0]["status"] == 200
    assert not (blobs(harness) / f"{old}.splashgui.incomplete").exists()
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


def test_a_hard_killed_manager_continues_the_file_after_a_restart(manager_process):
    manager, hub = manager_process, manager_process.hub
    hub.cdn_bps = 512 << 10
    manager.start()
    dl = manager.api("POST", "/downloads", {"id": MODEL})["id"]
    sha = remote_file().blob
    partial = manager.home / "models" / ("models--" + REPO_ID.replace("/", "--")) / "blobs"
    partial = partial / f"{sha}.splashgui.incomplete"
    deadline = time.monotonic() + 60
    while not (partial.exists() and partial.stat().st_size >= 1 << 20):
        assert time.monotonic() < deadline, "the Range download never got going"
        time.sleep(0.05)
    manager.kill()
    at_kill = partial.stat().st_size
    assert 0 < at_kill < SHARD
    before = len(cdn_gets(hub))

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
    after = cdn_gets(hub)[before:]
    assert after[0]["headers"].get("range") == f"bytes={at_kill}-"
    assert after[0]["status"] == 206
    blob = partial.with_name(sha)
    assert sha256(blob) == sha
    assert not partial.exists()
    assert "range GET" in (manager.home / "logs" / "manager.log").read_text()

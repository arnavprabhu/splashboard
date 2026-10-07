"""A compatibility check leaves nothing in the models folder (acceptance 1.3 F5).

Splash's check fetches `config.json` (and for some repositories a few other metadata
files) with `hf_hub_download`, which wrote a `models--<owner>--<repo>` folder into the
models folder for every repository the search or By ID checked. The manager removes what
a check created, only when it holds nothing but that metadata."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from splash_gui import hubcache
from splash_gui.models import inspection as ins

from .test_models_api import hub_harness  # noqa: F401

REPO_ID = "mlx-community/Qwen3.8-27B-8bit"
SHA = "815b83c0df8ffd1d1b5244cf75fd6ef14fca9ef9"


def fetch(models: Path, repo_id: str, name: str = "config.json", data: bytes = b"{}") -> Path:
    """What `hf_hub_download` leaves: a blob, a snapshot link to it, a lock file."""
    folder = hubcache.repo_folder(models, repo_id)
    digest = hashlib.sha1(data, usedforsecurity=False).hexdigest()  # a git blob id, as the Hub
    (folder / "blobs").mkdir(parents=True, exist_ok=True)
    (folder / "blobs" / digest).write_bytes(data)
    snapshot = folder / "snapshots" / SHA
    snapshot.mkdir(parents=True, exist_ok=True)
    if not (snapshot / name).is_symlink():
        (snapshot / name).symlink_to(f"../../blobs/{digest}")
    locks = models / ".locks" / folder.name
    locks.mkdir(parents=True, exist_ok=True)
    (locks / f"{digest}.lock").touch()
    return folder


def test_a_folder_the_check_created_is_removed_with_its_lock(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    before = hubcache.check_footprint(models, REPO_ID)
    folder = fetch(models, REPO_ID)
    fetch(models, REPO_ID, "model.safetensors.index.json", b'{"weight_map": {}}')
    (folder / ".no_exist" / SHA).mkdir(parents=True)
    (folder / ".no_exist" / SHA / "preprocessor_config.json").touch()
    assert hubcache.remove_check_leftover(models, REPO_ID, before) is True
    assert sorted(p.name for p in models.rglob("*")) == [".locks"]


def test_what_existed_before_the_check_is_kept(tmp_path: Path) -> None:
    models = tmp_path / "models"
    fetch(models, REPO_ID)
    before = hubcache.check_footprint(models, REPO_ID)
    assert before == (True, True)
    assert hubcache.remove_check_leftover(models, REPO_ID, before) is False
    assert (hubcache.repo_folder(models, REPO_ID) / "snapshots" / SHA / "config.json").exists()

    # A new folder, but the lock directory was there already: only the folder goes.
    other = "owner/other"
    locks = models / ".locks" / hubcache.repo_folder(models, other).name
    locks.mkdir(parents=True)
    before = hubcache.check_footprint(models, other)
    fetch(models, other)
    assert hubcache.remove_check_leftover(models, other, before) is True
    assert locks.is_dir()


@pytest.mark.parametrize(
    "extra",
    [
        "weights",  # a snapshot file the check never fetches
        "partial",  # a download in progress
        "unlinked",  # a complete blob no snapshot points at yet
        "stray",  # anything else in the folder
    ],
)
def test_a_folder_with_anything_else_is_kept(tmp_path: Path, extra: str) -> None:
    models = tmp_path / "models"
    models.mkdir()
    before = hubcache.check_footprint(models, REPO_ID)
    folder = fetch(models, REPO_ID)
    digest = "a" * 40
    if extra == "weights":
        fetch(models, REPO_ID, "model.safetensors", b"weights")
    elif extra == "partial":
        (folder / "blobs" / f"{digest}.0123abcd.incomplete").write_bytes(b"x")
    elif extra == "unlinked":
        (folder / "blobs" / digest).write_bytes(b"x")
    else:
        (folder / "notes.txt").write_text("mine")
    assert hubcache.remove_check_leftover(models, REPO_ID, before) is False
    assert folder.is_dir()


def test_inspect_leaves_no_folder_behind(hub_harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """End to end through POST /inspect: the helper's metadata fetch (simulated
    here, as the fake engine reads the fake Hub without a cache) is removed when the
    check created the folder, kept when the folder was there before, and kept while
    the repository downloads."""
    h = hub_harness()
    models = h.state.settings.models_dir()
    real = ins.run_helper

    async def helper_that_fetches(run: Any, *args: Any) -> None:
        fetch(models, run.repo_id)
        await real(run, *args)

    monkeypatch.setattr(ins, "run_helper", helper_that_fetches)
    folder = hubcache.repo_folder(models, REPO_ID)

    result = h.client.post(f"/api/admin/inspect?id={REPO_ID}&refresh=1").json()
    assert result["compatible"] is False, "the 8-bit check itself still answers"
    assert not folder.exists(), "the check created the folder, so it is gone"
    assert not (models / ".locks" / folder.name).exists()

    # Already there (someone's earlier fetch, or a model): kept as it was.
    fetch(models, REPO_ID)
    h.client.post(f"/api/admin/inspect?id={REPO_ID}&refresh=1")
    assert (folder / "snapshots" / SHA / "config.json").exists()

    # A download of the repository owns the folder: never touched while it runs.
    import shutil

    shutil.rmtree(folder)
    downloads = h.state.downloads
    monkeypatch.setattr(
        downloads,
        "items",
        {"d1": type("Item", (), {"model": REPO_ID, "state": "running"})()},
    )
    h.client.post(f"/api/admin/inspect?id={REPO_ID}&refresh=1")
    assert folder.is_dir()

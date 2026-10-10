"""huggingface_hub cache layout, shared by the Downloader and the engine
supervisor's install progress and stale-partial cleanup.

A blob is `models--OWNER--REPO/blobs/<etag>`. While it downloads, huggingface_hub
before 1.x wrote `<etag>.incomplete` and continued it on the next run; 1.28 (bundled
with Splash 1.2.0) writes a per-process `<etag>.<uuid8>.incomplete`, opened `"wb"`,
renamed to the blob when complete and deleted on a handled error
(`huggingface_hub/file_download.py` `_download_to_tmp_and_move`, PR #4228) **[code]**.
A killed run leaves its uuid partial behind, and no later run can reuse it.

The manager's own byte-resumable downloads of large LFS/Xet files
(`downloads/ranged.py`) write `<etag>.splashgui.incomplete` next to a sidecar
`<etag>.splashgui.json` that records which remote file the bytes belong to (up
to four Range segments fill one preallocated partial, and the sidecar records each
segment's verified bytes, which are its progress). The next run continues each
segment from its verified end (a one-stream partial from its size), so the partial is
kept across pause and restart, counted as progress, and deleted on cancel or once
nothing claims it.
"""

from __future__ import annotations

import contextlib
import glob
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_DIGEST = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
UUID_PARTIAL = re.compile(rf"^(?P<digest>{_DIGEST})\.(?P<uuid>[0-9a-f]{{8}})\.incomplete$")
LEGACY_PARTIAL = re.compile(rf"^(?P<digest>{_DIGEST})\.incomplete$")
RANGE_SUFFIX = ".splashgui.incomplete"
RANGE_SIDECAR_SUFFIX = ".splashgui.json"
RANGE_PARTIAL = re.compile(rf"^(?P<digest>{_DIGEST})\.splashgui\.incomplete$")
RANGE_SIDECAR = re.compile(rf"^(?P<digest>{_DIGEST})\.splashgui\.json$")
BLOB = re.compile(rf"^{_DIGEST}$")


def repo_folder(models_dir: Path, repo_id: str) -> Path:
    return models_dir / ("models--" + repo_id.replace("/", "--"))


def blobs_dir(models_dir: Path, repo_id: str) -> Path:
    return repo_folder(models_dir, repo_id) / "blobs"


def range_partial(blob: Path) -> Path:
    """The manager's resumable partial for a blob."""
    return blob.with_name(blob.name + RANGE_SUFFIX)


def range_sidecar(blob: Path) -> Path:
    """What the resumable partial's bytes belong to (`downloads/ranged.py`)."""
    return blob.with_name(blob.name + RANGE_SIDECAR_SUFFIX)


def blob_partials(blob: Path) -> list[Path]:
    """A blob's partial files: the legacy `<etag>.incomplete` first, then every
    per-process `<etag>.<uuid8>.incomplete` and the manager's resumable
    `<etag>.splashgui.incomplete`."""
    legacy = blob.with_name(blob.name + ".incomplete")
    found = [legacy] if legacy.is_file() else []
    found += sorted(blob.parent.glob(glob.escape(blob.name) + ".*.incomplete"))
    return found


def remove_partial(path: Path) -> None:
    """Delete a partial file, and with the manager's resumable one its sidecar too."""
    path.unlink(missing_ok=True)
    match = RANGE_PARTIAL.match(path.name)
    if match:
        path.with_name(match.group("digest") + RANGE_SIDECAR_SUFFIX).unlink(missing_ok=True)


def partial_digest(name: str) -> str | None:
    """The blob digest a partial file belongs to (any naming), else None."""
    match = UUID_PARTIAL.match(name) or LEGACY_PARTIAL.match(name) or RANGE_PARTIAL.match(name)
    return match.group("digest") if match else None


@dataclass(frozen=True)
class FileFact:
    size: int
    mtime: float


def scan_blobs(directory: Path) -> dict[str, FileFact]:
    """Every file in a blobs directory (complete blobs and partials) with its size."""
    out: dict[str, FileFact] = {}
    try:
        entries = list(directory.iterdir())
    except OSError:
        return out
    for entry in entries:
        with contextlib.suppress(OSError):
            info = entry.stat()
            out[entry.name] = FileFact(info.st_size, info.st_mtime)
    return out


def open_paths(paths: list[Path], timeout: float = 5.0) -> set[str] | None:
    """The subset of `paths` some process has open, from `lsof`; None when that
    cannot be told (lsof missing or timed out), so callers keep everything."""
    if not paths:
        return set()
    try:
        result = subprocess.run(
            ["lsof", "-F", "n", "--", *(str(p) for p in paths)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # lsof exits 1 when none of the files is open (and prints nothing).
    if result.returncode not in (0, 1):
        return None
    wanted = {str(p) for p in paths}
    resolved = {str(p.resolve()): str(p) for p in paths}
    found: set[str] = set()
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            name = line[1:]
            if name in wanted:
                found.add(name)
            elif name in resolved:
                found.add(resolved[name])
    return found


# What a compatibility check fetches into the Hub cache: Splash's `Repository.file`
# (`hf_hub_download`) for these metadata files (install/upstream.py, and a Splash
# package's manifest, read to refuse it). Weights and GGUF headers are read in place
# over HTTP range requests (`Repository.open`), so a check downloads nothing else.
CHECK_FILES = frozenset(
    {
        "config.json",
        "preprocessor_config.json",
        "processor_config.json",
        "model.safetensors.index.json",
        "manifest.json",
    }
)


def check_only(folder: Path) -> bool:
    """Whether a repository folder holds only what a compatibility check fetches:
    snapshot links to `CHECK_FILES`, the blobs they point at, `refs/` and
    `.no_exist/` markers. Any other file, a partial or an unlinked blob (a download
    in progress) means it is someone's data."""
    if not folder.is_dir() or folder.is_symlink():
        return False
    blobs: set[str] = set()
    linked: set[str] = set()
    for path in folder.rglob("*"):
        parts = path.relative_to(folder).parts
        top = parts[0]
        if top in ("refs", ".no_exist"):
            continue
        if len(parts) == 1 and top in ("blobs", "snapshots") and path.is_dir():
            continue
        if top == "snapshots" and len(parts) == 2 and path.is_dir() and not path.is_symlink():
            continue
        if top == "snapshots" and len(parts) == 3 and parts[2] in CHECK_FILES:
            if not path.is_symlink():
                return False
            target = Path(path.readlink())
            if target.parent.name != "blobs":
                return False
            linked.add(target.name)
            continue
        if top == "blobs" and len(parts) == 2 and BLOB.match(path.name) and path.is_file():
            blobs.add(path.name)
            continue
        return False
    return blobs <= linked


def check_footprint(models_dir: Path, repo_id: str) -> tuple[bool, bool]:
    """Whether the repository folder and its `.locks/` entry exist, read before a
    compatibility check so that afterwards only what it created is removed."""
    folder = repo_folder(models_dir, repo_id)
    return folder.exists(), (models_dir / ".locks" / folder.name).exists()


def remove_check_leftover(models_dir: Path, repo_id: str, before: tuple[bool, bool]) -> bool:
    """Delete what a compatibility check created in the Hub cache (acceptance 1.3 F5):
    the repository folder, only if it did not exist before (`before`, from
    `check_footprint`) and holds nothing but the check's own metadata
    (`check_only`), and then its `.locks/` entry if that is new too and holds only
    lock files."""
    folder_existed, locks_existed = before
    folder = repo_folder(models_dir, repo_id)
    if folder_existed or not check_only(folder):
        return False
    shutil.rmtree(folder)
    locks = models_dir / ".locks" / folder.name
    with contextlib.suppress(OSError):
        if (
            not locks_existed
            and locks.is_dir()
            and all(p.suffix == ".lock" for p in locks.iterdir())
        ):
            shutil.rmtree(locks)
    return True

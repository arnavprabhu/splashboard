"""huggingface_hub cache layout, shared by the Downloader (SPEC §9.4) and the engine
supervisor's install progress and stale-partial cleanup (§6.3).

A blob is `models--OWNER--REPO/blobs/<etag>`. While it downloads, huggingface_hub
before 1.x wrote `<etag>.incomplete` and continued it on the next run; 1.28 (bundled
with Splash 1.2.0) writes a per-process `<etag>.<uuid8>.incomplete`, opened `"wb"`,
renamed to the blob when complete and deleted on a handled error
(`huggingface_hub/file_download.py` `_download_to_tmp_and_move`, PR #4228) **[code]**.
A killed run leaves its uuid partial behind, and no later run can reuse it.
"""

from __future__ import annotations

import contextlib
import glob
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_DIGEST = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
UUID_PARTIAL = re.compile(rf"^(?P<digest>{_DIGEST})\.(?P<uuid>[0-9a-f]{{8}})\.incomplete$")
LEGACY_PARTIAL = re.compile(rf"^(?P<digest>{_DIGEST})\.incomplete$")
BLOB = re.compile(rf"^{_DIGEST}$")


def repo_folder(models_dir: Path, repo_id: str) -> Path:
    return models_dir / ("models--" + repo_id.replace("/", "--"))


def blobs_dir(models_dir: Path, repo_id: str) -> Path:
    return repo_folder(models_dir, repo_id) / "blobs"


def blob_partials(blob: Path) -> list[Path]:
    """A blob's partial files: the legacy `<etag>.incomplete` first, then every
    per-process `<etag>.<uuid8>.incomplete`."""
    legacy = blob.with_name(blob.name + ".incomplete")
    found = [legacy] if legacy.is_file() else []
    found += sorted(blob.parent.glob(glob.escape(blob.name) + ".*.incomplete"))
    return found


def partial_digest(name: str) -> str | None:
    """The blob digest a partial file belongs to (either naming), else None."""
    match = UUID_PARTIAL.match(name) or LEGACY_PARTIAL.match(name)
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

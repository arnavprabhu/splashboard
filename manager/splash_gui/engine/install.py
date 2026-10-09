"""What `splash serve` downloads before it loads (`starting.installing`).

Splash's launcher runs the installer when the selection is missing, damaged or has
moved to a new commit. For each repository with files to fetch it prints
`Fetching N file(s), X GB, from REPO@REV12; cached files are reused.`
(splash/install/hub.py:352 `Repository.download`, X = the uncached files' bytes / 1e9
to two places) and then calls `snapshot_download` with four workers (hub.py:357-362),
so several partials grow at once. The draft's repository is fetched first and the
target's after it (install/upstream.py `_install`: `_draft(...)` at :428 downloads the
draft through `_draft_files` → `repo.download` at :586, before
`repo.download(target.files)` at :435); a vision tower or projector lives in the
target repository (upstream.py:159-161, :204-210), so it is part of the target's
line. Each line starts a new tracker: progress is per repository,
in the order Splash fetches them (`EngineView.install`). Splash prints no progress of
its own, so the manager measures it from the Hub cache, as the Downloader does:
bytes in the per-process `<etag>.<uuid8>.incomplete` partials this run
created, plus blobs completed since the line.

A run that is stopped mid-file loses that file: huggingface_hub 1.28 cannot continue
a partial. `remove_stale_partials` deletes what killed runs left behind, and the
Downloader's resumable `<etag>.splashgui.incomplete` partials once no unfinished
download claims them.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..hubcache import (
    BLOB,
    RANGE_PARTIAL,
    RANGE_SIDECAR,
    RANGE_SUFFIX,
    UUID_PARTIAL,
    FileFact,
    blobs_dir,
    open_paths,
    partial_digest,
    remove_partial,
    scan_blobs,
)
from ..schemas import EngineInstall

log = logging.getLogger(__name__)

# splash/install/hub.py:352-355 (1.2.0); the revision is the commit's first 12 digits.
FETCHING = re.compile(
    r"Fetching (?P<files>\d+) file\(s\), (?P<gb>\d+(?:\.\d+)?) GB, "
    r"from (?P<repo>[^@\s]+)@(?P<rev>[0-9A-Za-z]+); cached files are reused"
)

# A partial written within this many seconds counts as live when lsof cannot tell.
RECENT_WRITE_S = 120.0


@dataclass(frozen=True)
class FetchLine:
    repo: str
    revision: str
    files: int
    total_bytes: int


def parse_fetching(line: str) -> FetchLine | None:
    match = FETCHING.search(line)
    if match is None:
        return None
    return FetchLine(
        repo=match.group("repo"),
        revision=match.group("rev"),
        files=int(match.group("files")),
        # Splash prints decimal GB with two places: the total is known to ±5 MB.
        total_bytes=round(float(match.group("gb")) * 1e9),
    )


# A blob already in the cache when the tracker starts still counts as fetched by this
# line when it was written this recently before the line was read (a small file that
# finished between Splash printing the line and the manager scanning the blobs).
LINE_SLACK_S = 2.0


@dataclass
class InstallTracker:
    """Progress of one `Fetching …` line, sampled from the repository's blobs.

    Limits:
    - Blobs and partials of digests an unfinished Downloader item owns
      (`excluded()`, `Downloads.active_digests`) are left out, so a concurrent
      download of the same repository doesn't inflate this one.
    - A blob completed between the line and the first scan counts when its mtime
      (the rename keeps the partial's last write) is at most `LINE_SLACK_S` before the
      line was read (`line_wall`); one finished earlier than that is taken as cached.
    - Progress is the partials' file size. With hf_xet (1.6.0, bundled; used for
      Xet-backed repositories, `file_download.py` `xet_get`) the writer is a compiled
      Rust extension whose source is not bundled, so whether it preallocates or
      writes out of order cannot be told from the package; if it does, progress
      would run ahead of the bytes actually received.
    """

    models_dir: Path
    fetch: FetchLine
    clock: Callable[[], float] = time.monotonic
    excluded: Callable[[], set[str]] = set
    line_wall: float | None = None
    baseline: dict[str, FileFact] = field(default_factory=dict)
    done_bytes: int = 0
    speed_bps: float | None = None
    _last: tuple[float, int] | None = None

    def __post_init__(self) -> None:
        self.baseline = scan_blobs(self.directory)
        if self.line_wall is not None:
            # Fetched by this line, just before the scan: not cached.
            self.baseline = {
                name: fact
                for name, fact in self.baseline.items()
                if not (BLOB.match(name) and fact.mtime >= self.line_wall - LINE_SLACK_S)
            }
        self._last = (self.clock(), 0)

    @property
    def directory(self) -> Path:
        return blobs_dir(self.models_dir, self.fetch.repo)

    def sample(self) -> None:
        files = scan_blobs(self.directory)
        try:
            others = self.excluded()
        except Exception:
            others = set()
        done = 0
        partials: dict[str, int] = {}
        for name, fact in files.items():
            before = self.baseline.get(name)
            if BLOB.match(name):
                if before is None and name not in others:
                    done += fact.size  # completed since the line
                continue
            digest = partial_digest(name)
            if digest is None or digest in others:
                continue
            # A partial counts when this run created it, or when it was already
            # growing as the line was read (it was created a moment before).
            if before is not None and fact.size <= before.size:
                continue
            partials[digest] = max(partials.get(digest, 0), fact.size)
        done += sum(size for digest, size in partials.items() if digest not in files)
        # Never backwards (a partial renamed between two listings is briefly in neither).
        self.done_bytes = max(done, self.done_bytes)
        now = self.clock()
        if self._last is not None:
            then, previous = self._last
            if now - then >= 0.2:
                speed = max(0, self.done_bytes - previous) / (now - then)
                previous_speed = self.speed_bps
                self.speed_bps = (
                    speed if previous_speed is None else previous_speed * 0.8 + speed * 0.2
                )
                self._last = (now, self.done_bytes)

    def view(self) -> EngineInstall:
        # The printed total is rounded to 10 MB (and is 0.00 GB for small files).
        total = max(self.fetch.total_bytes, self.done_bytes)
        remaining = max(0, total - self.done_bytes)
        eta = remaining / self.speed_bps if self.speed_bps else None
        return EngineInstall(
            repo=self.fetch.repo,
            revision=self.fetch.revision,
            files=self.fetch.files,
            total_bytes=total,
            done_bytes=self.done_bytes,
            speed_bps=round(self.speed_bps, 1) if self.speed_bps is not None else None,
            eta_s=round(eta, 1) if eta is not None else None,
        )


@dataclass(frozen=True)
class Removed:
    path: Path
    size: int


def remove_stale_partials(
    models_dir: Path,
    repos: Iterable[str],
    *,
    protected_digests: set[str],
    now: Callable[[], float] = time.time,
    lsof: Callable[[list[Path]], set[str] | None] = open_paths,
) -> list[Removed]:
    """Delete the partials in `repos`' blobs that nothing will continue: the
    per-process `<etag>.<uuid8>.incomplete` files, and the Downloader's resumable
    `<etag>.splashgui.incomplete` (with its sidecar) once no unfinished download
    claims it. Kept: the legacy `<etag>.incomplete` (an older hub resumes it), any
    file a process has open, and any blob an unfinished download in the Downloader
    needs (`protected_digests`). When `lsof` cannot tell, a partial written in the
    last two minutes is kept too. A sidecar left without its partial goes as well."""
    candidates: list[tuple[Path, FileFact]] = []
    for repo in dict.fromkeys(repos):
        directory = blobs_dir(models_dir, repo)
        files = scan_blobs(directory)
        for name, fact in files.items():
            # Never the legacy `<etag>.incomplete`.
            match = UUID_PARTIAL.match(name) or RANGE_PARTIAL.match(name)
            if match is None:
                sidecar = RANGE_SIDECAR.match(name)
                if (
                    sidecar
                    and sidecar.group("digest") not in protected_digests
                    and sidecar.group("digest") + RANGE_SUFFIX not in files
                ):
                    (directory / name).unlink(missing_ok=True)
                continue
            if match.group("digest") in protected_digests:
                continue
            candidates.append((directory / name, fact))
    if not candidates:
        return []
    busy = lsof([path for path, _ in candidates])
    removed: list[Removed] = []
    for path, fact in candidates:
        if busy is None:
            if now() - fact.mtime < RECENT_WRITE_S:
                continue  # cannot tell whether it is open: a recent write may be live
        elif str(path) in busy:
            continue
        if not path.exists():
            continue
        try:
            remove_partial(path)
        except OSError as error:
            log.warning("could not remove stale partial %s: %s", path, error)
            continue
        removed.append(Removed(path, fact.size))
    return removed

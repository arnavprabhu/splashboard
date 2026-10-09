"""Byte-level resumable downloads of large Hub files.

Splash's installer (`install/models.py prepare`, huggingface_hub 1.28) starts a file
it did not finish from byte 0 in a new `<etag>.<uuid8>.incomplete`. For LFS/Xet files
of `RANGE_MIN_BYTES` or more, the Downloader therefore fetches the bytes itself with
HTTP Range requests into the same Hub cache first, and then runs `prepare` as before;
`prepare` finds the blob and its snapshot pointer and fetches only the small files.

Data shape, per file (next to the blob it becomes, in `models--O--R/blobs/`):

- **partial** `<etag>.splashgui.incomplete`. Version 2 is preallocated to the
  file's full size, a sparse file, and filled by `RANGE_SEGMENTS` Range streams at
  once, each at its own offsets. Version 1 is one stream whose size is the bytes done.
- **sidecar** `<etag>.splashgui.json` (`PartialState`): what the bytes belong to (repo,
  path, commit, etag = the sha256 that names the blob, size, Xet hash, the CDN's
  validator) and, for version 2, each segment's `[start, end)` and `verified_end`:
  the bytes below `verified_end` are in the partial. A sidecar without `version` is
  version 1 (everything written before parallel streams). The sidecar is one 4 KiB record,
  rewritten in place after every chunk, so a hard kill loses at most the chunk in
  flight for each segment and never re-fetches a verified byte.
- **lock** `../.locks/models--O--R/<etag>.lock`: `flock`, the lock huggingface_hub
  holds while it writes that blob.

How the Downloader's job maps onto it (`downloads/service.py`):

- **Progress and speed** are `partial_progress`: a version 2 partial's verified bytes
  from the sidecar (its file size is the full size), a version 1 partial's size. Read
  every 500 ms like Splash's own partials.
- **Pause** cancels the job's task: the streams stop, the lock is released, the
  partial and sidecar stay.
- **Resume**, and a manager restart (the queue comes back `queued`), run the job
  again: the file is resolved again, and a sidecar that matches the Hub's etag, size
  and Xet hash continues every segment from its `verified_end` (or a version 1 partial
  from its size). Anything else starts again.
- **Cancel** deletes the partial and its sidecar (`hubcache.remove_partial`).
- **Done**: the sha256 of every byte is checked before the atomic rename to
  `blobs/<etag>`; then the `snapshots/<commit>/<path>` link and `refs/<branch>` are
  written the way huggingface_hub writes them.
- **One stream instead**: a segment that gets `200` for its Range, a
  `Content-Range` that does not start at its verified end or runs past its end, a
  changed validator, or more bytes than its `Content-Range` restarts the file as one
  stream from byte 0. The job log says so. A shorter answer than asked is fine: the
  segment asks for the rest.
- **Any other problem** (a hash mismatch, an unexpected status, missing metadata, a
  lock held for `LOCK_TIMEOUT_S`, repeated network errors) raises `Fallback`, and the
  job leaves that file to Splash's `prepare`: the worst case is the per-file behaviour
  Splash has anyway. A hash mismatch also deletes the partial.

The token goes only to the Hub's own host (`hf.endpoint`), never to the CDN that
`Location` points at; redirects are followed by hand. Logged URLs drop their query
string (a signed CDN URL is a credential for an hour) and the token is never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from ..hubcache import (
    RANGE_PARTIAL,
    RANGE_SIDECAR_SUFFIX,
    range_partial,
    range_sidecar,
    remove_partial,
    repo_folder,
)

log = logging.getLogger(__name__)

# Files this large or larger are fetched with Range requests. A fixed constant,
# not a setting: every supported GGUF and most MLX shards are above it, and the
# small files are cheap to restart.
RANGE_MIN_BYTES = 1_000_000_000
# Parallel Range streams per file, each into its own part of the partial. A
# constant, not a setting. Measured live (2026-10-07, build/verify/q40/results.txt):
# median 37.7 MB/s with 1 segment, 53.0 with 4, 56.5 with 8; 4 stays.
RANGE_SEGMENTS = 4
# The sidecar is one fixed-size record, rewritten in place after every chunk.
SIDECAR_BYTES = 4096
# How long to wait for another writer of the same blob (Splash's own installer, or a
# second download sharing a draft) before leaving the file to `prepare`, which waits on
# the same lock and then finds the blob.
LOCK_TIMEOUT_S = 60.0
# Network errors and expired CDN URLs retried in a row before falling back.
RETRIES = 5
RETRY_DELAY_S = 2.0
USER_AGENT = "splash-gui"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")


class Fallback(Exception):
    """This file is left to Splash's `prepare`; the message says why, in plain words."""


def safe_relative(name: str) -> bool:
    """Whether a repository file name or revision stays inside the folder it is joined to:
    relative, forward slashes only, no empty, `.` or `..` part and no control characters.
    The names come from the Hub (or a mirror set in `hf.endpoint`), so they are checked
    before they become paths."""
    if not name or name.startswith("/") or "\\" in name:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


class _Mismatch(Exception):
    """The server did not answer a segment the way the segmented plan needs. The file
    starts again as one stream; the message says what was wrong."""


class _Expired(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"the download URL answered {status} (expired)")


@dataclass(frozen=True)
class Remote:
    """What the Hub says about one file: `HEAD …/resolve/<revision>/<path>`."""

    repo: str
    revision: str
    path: str
    commit: str
    etag: str  # X-Linked-Etag: the LFS sha256, which names the blob
    size: int  # X-Linked-Size
    location: str  # the CDN URL (for Xet files a signed xet-bridge URL, 1 h)
    xet_hash: str | None


@dataclass
class Segment:
    """One Range stream of a version 2 partial: bytes `[start, end)`, of which
    `[start, verified_end)` are in the partial."""

    start: int
    end: int
    verified_end: int


@dataclass
class PartialState:
    """The sidecar: what the bytes in the partial belong to, and (version 2) how far
    each segment has got."""

    repo: str
    path: str
    commit: str
    etag: str
    size: int
    xet_hash: str | None
    validator: str | None  # the CDN's ETag, sent back as If-Range
    version: int = 1  # 1: one stream, the partial's size is the bytes done; 2: segments
    segments: list[Segment] = field(default_factory=list)

    @classmethod
    def for_remote(
        cls, remote: Remote, *, version: int, segments: list[Segment] | None = None
    ) -> PartialState:
        return cls(
            remote.repo,
            remote.path,
            remote.commit,
            remote.etag,
            remote.size,
            remote.xet_hash,
            None,
            version,
            segments or [],
        )

    def matches(self, remote: Remote) -> bool:
        return (self.etag, self.size, self.xet_hash) == (remote.etag, remote.size, remote.xet_hash)


def is_candidate(digest: str | None, size: int | None) -> bool:
    """Whether a planned file is fetched with Range: an LFS/Xet file (its blob is
    named by a sha256) of at least `RANGE_MIN_BYTES`."""
    return bool(digest and _SHA256.match(digest)) and (size or 0) >= RANGE_MIN_BYTES


def redact_url(url: str) -> str:
    """Scheme, host and path only: a CDN query string is a signed credential."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" + ("?[signed]" if parts.query else "")


def _etag(value: str | None) -> str | None:
    return value.removeprefix("W/").strip('"') if value else None


def _encode(state: PartialState) -> bytes:
    """The sidecar's bytes: JSON padded with spaces to `SIDECAR_BYTES`, so a version 2
    record can be rewritten in place with the same length."""
    data = json.dumps(asdict(state)).encode()
    if len(data) > SIDECAR_BYTES:
        raise ValueError("the download sidecar does not fit its record")
    return data.ljust(SIDECAR_BYTES)


def _read_state(path: Path) -> PartialState | None:
    """The sidecar, or None when it is missing, unreadable or of a version this build
    does not know. Sidecars written before parallel streams have no `version` and are one stream."""
    try:
        raw = json.loads(path.read_text())
        version = raw.pop("version", 1)
        segments = [Segment(**segment) for segment in raw.pop("segments", [])]
        if version not in (1, 2):
            return None
        return PartialState(version=version, segments=segments, **raw)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _replace(path: Path, data: bytes) -> None:
    """Atomic write with the default (umask) mode, as huggingface_hub writes refs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def partial_progress(partial: Path) -> int:
    """The bytes a manager partial holds that a resume keeps: the verified bytes of a
    version 2 sidecar, the size of a version 1 partial, and 0 when no sidecar claims
    the partial. A version 2 partial's file size is the full size, so progress never
    reads it."""
    try:
        size = partial.stat().st_size
    except OSError:
        return 0
    match = RANGE_PARTIAL.match(partial.name)
    if match is None:
        return size
    state = _read_state(partial.with_name(match.group("digest") + RANGE_SIDECAR_SUFFIX))
    if state is None:
        return 0
    if state.version == 2:
        return sum(segment.verified_end - segment.start for segment in state.segments)
    return size


def _plan_segments(size: int) -> list[Segment]:
    """`RANGE_SEGMENTS` equal ranges of `size` bytes, none empty, nothing verified."""
    bounds = [size * index // RANGE_SEGMENTS for index in range(RANGE_SEGMENTS + 1)]
    return [Segment(start, end, start) for start, end in pairwise(bounds) if end > start]


def _covers(state: PartialState, size: int) -> bool:
    """Whether the segments tile `[0, size)` in order and each is within its bounds."""
    segments = state.segments
    if not segments or segments[0].start != 0 or segments[-1].end != size:
        return False
    if any(not (s.start < s.end and s.start <= s.verified_end <= s.end) for s in segments):
        return False
    return all(a.end == b.start for a, b in pairwise(segments))


def _check_range(header: str | None, segment: Segment, size: int) -> int:
    """The answer to a segment's Range must start where the segment's verified bytes end
    and stay inside the segment. It may stop short of the segment's end (a server that
    caps each answer); the rest is asked for again. Returns the answer's last byte."""
    match = _CONTENT_RANGE.match(header or "")
    if (
        match is None
        or int(match.group(1)) != segment.verified_end
        or not segment.verified_end <= int(match.group(2)) <= segment.end - 1
        or match.group(3) not in ("*", str(size))
    ):
        raise _Mismatch(f"the server answered Content-Range {header!r}, not the range asked for")
    return int(match.group(2))


def _preallocate(partial: Path, size: int) -> None:
    """An empty partial of `size` bytes (sparse): the segments write into their ranges."""
    fd = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o666)
    try:
        os.ftruncate(fd, size)
    finally:
        os.close(fd)


def link_snapshot(folder: Path, remote: Remote) -> None:
    """`snapshots/<commit>/<path>` → the relative path of `blobs/<etag>`, and
    `refs/<revision>` when the revision is a branch or tag (huggingface_hub
    `_create_symlink`, `_cache_commit_hash_for_specific_revision`)."""
    snapshots = folder / "snapshots"
    pointer = snapshots / remote.commit / remote.path
    ref = folder / "refs" / remote.revision
    # `remote` checks these names; this keeps a bad one from ever leaving the repo folder.
    for path, root in ((pointer, snapshots), (ref, folder / "refs")):
        if not path.resolve().is_relative_to(root.resolve()):
            raise Fallback(f"{path} is outside {root}")
    pointer.parent.mkdir(parents=True, exist_ok=True)
    target = os.path.relpath(folder / "blobs" / remote.etag, pointer.parent)
    if not (pointer.is_symlink() and str(pointer.readlink()) == target):
        pointer.unlink(missing_ok=True)
        pointer.symlink_to(target)
    stale = not ref.is_file() or ref.read_text() != remote.commit
    if remote.revision != remote.commit and stale:
        _replace(ref, remote.commit.encode())


def _hash_prefix(path: Path, length: int) -> Any:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        remaining = length
        while remaining:
            chunk = source.read(min(8 << 20, remaining))
            if not chunk:
                break
            hasher.update(chunk)
            remaining -= len(chunk)
    return hasher


def _sync(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


class _Journal:
    """A version 2 partial and its sidecar, open while the segments run. A chunk is
    written at its offset first and the sidecar's record of it after, so the record
    never claims a byte that is not in the partial."""

    def __init__(self, partial: Path, sidecar: Path, state: PartialState) -> None:
        self.state = state
        self._data = os.open(partial, os.O_RDWR)
        try:
            self._record = os.open(sidecar, os.O_RDWR)
        except OSError:
            os.close(self._data)
            raise

    def write(self, offset: int, chunk: bytes) -> None:
        view = memoryview(chunk)
        while view:
            written = os.pwrite(self._data, view, offset)
            view = view[written:]
            offset += written

    def checkpoint(self) -> None:
        os.pwrite(self._record, _encode(self.state), 0)

    def close(self) -> None:
        for fd in (self._data, self._record):
            with contextlib.suppress(OSError):
                os.close(fd)


@dataclass
class _Link:
    """The CDN URL the segments share. A signed URL expires; the first segment that
    sees it expire replaces it for the others."""

    url: str


async def _join(tasks: list[asyncio.Task[None]]) -> None:
    """Wait for every segment. The first failure cancels the rest and is raised."""
    try:
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_EXCEPTION)
            failed: list[BaseException] = [
                error for task in done if not task.cancelled() and (error := task.exception())
            ]
            if failed:
                raise failed[0]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class RangeDownloader:
    """Fetches large files of a repository into the Hub cache at `models_dir`.

    `note` receives one plain line per step for the job's log; the manager's log gets
    every request with its status (URLs without their query string)."""

    def __init__(
        self,
        models_dir: Path,
        endpoint: str,
        token: str | None,
        *,
        note: Callable[[str], None] = lambda _line: None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.models_dir = models_dir
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.note = note
        self.transport = transport

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(30, read=60),
            transport=self.transport,
            follow_redirects=False,
        )

    def _clean(self, text: str) -> str:
        text = re.sub(r"(https?://[^\s?'\"]+)\?[^\s'\"]*", r"\1?[signed]", text)
        return text.replace(self.token, "[redacted]") if self.token else text

    def _headers(self, url: str) -> dict[str, str]:
        headers = {"Accept-Encoding": "identity", "User-Agent": USER_AGENT}
        hub = urlsplit(self.endpoint)
        target = urlsplit(url)
        if self.token and (target.scheme, target.netloc) == (hub.scheme, hub.netloc):
            # Only the Hub itself gets the token, never the CDN.
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def resolve(
        self, client: httpx.AsyncClient, repo: str, revision: str | None, path: str
    ) -> Remote:
        """HEAD the resolve URL, following only relative (same-Hub) redirects, as
        huggingface_hub's `_httpx_follow_relative_redirects_with_backoff` does, and read
        the headers its `get_hf_file_metadata` reads."""
        revision = revision or "main"
        if not safe_relative(path) or not safe_relative(revision):
            raise Fallback(f"the file name {path!r} or revision {revision!r} is not a safe path")
        url = f"{self.endpoint}/{repo}/resolve/{quote(revision, safe='')}/{quote(path)}"
        try:
            for _ in range(5):
                response = await client.head(url, headers=self._headers(url))
                log.info(
                    "range HEAD %s -> %s (x-linked-size %s, location %s)",
                    redact_url(url),
                    response.status_code,
                    response.headers.get("x-linked-size"),
                    redact_url(response.headers.get("location", "")) or "-",
                )
                if response.status_code in (401, 403):
                    raise Fallback(f"{response.status_code}: gated or private repository")
                location = response.headers.get("location")
                if response.is_redirect and location and not urlsplit(location).netloc:
                    url = urljoin(url, location)
                    continue
                break
        except httpx.HTTPError as error:
            raise Fallback(f"the Hub is unreachable: {self._clean(str(error))}") from None
        if response.status_code >= 400:
            raise Fallback(f"the Hub answered {response.status_code} for {path}")
        headers = response.headers
        etag = _etag(headers.get("x-linked-etag"))
        size = headers.get("x-linked-size")
        commit = headers.get("x-repo-commit")
        location = headers.get("location")
        if not etag or not _SHA256.match(etag) or not size or not size.isdigit():
            raise Fallback(f"the Hub did not describe {path} as an LFS file")
        if not commit or not location:
            raise Fallback(f"the Hub's answer for {path} has no commit or download URL")
        if not _COMMIT.match(commit):
            raise Fallback(f"the Hub's answer for {path} names an invalid commit")
        return Remote(
            repo=repo,
            revision=revision,
            path=path,
            commit=commit,
            etag=etag,
            size=int(size),
            location=urljoin(url, location),
            xet_hash=headers.get("x-xet-hash"),
        )

    @contextlib.asynccontextmanager
    async def hub_lock(self, repo: str, etag: str) -> AsyncIterator[None]:
        """huggingface_hub's blob lock (`.locks/<repo folder>/<etag>.lock`, filelock's
        UnixFileLock = flock), held for the whole download as the hub holds it."""
        path = self.models_dir / ".locks" / repo_folder(self.models_dir, repo).name / f"{etag}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o664)
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT_S
            waiting = False
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not waiting:
                        waiting = True
                        log.info("range lock wait %s", path)
                        self.note("Another download is writing this file; waiting for it.")
                    if time.monotonic() > deadline:
                        raise Fallback(
                            f"another download held the file's lock for {LOCK_TIMEOUT_S:.0f} s"
                        ) from None
                    await asyncio.sleep(0.25)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    async def download(self, client: httpx.AsyncClient, remote: Remote) -> Path:
        """Fetch `remote` into `blobs/<etag>` (continuing a matching partial), then link
        it into the snapshot. Raises `Fallback`; cancellation keeps the partial."""
        folder = repo_folder(self.models_dir, remote.repo)
        blob = folder / "blobs" / remote.etag
        blob.parent.mkdir(parents=True, exist_ok=True)
        async with self.hub_lock(remote.repo, remote.etag):
            if blob.is_file() and blob.stat().st_size == remote.size:
                remove_partial(range_partial(blob))
                link_snapshot(folder, remote)
                self.note(f"{remote.path} is already in the models folder.")
                return blob
            partial, sidecar = range_partial(blob), range_sidecar(blob)
            state = self._plan(remote, partial, sidecar)
            hasher: Any = None  # set when the bytes were hashed as they arrived
            if state.version == 2:
                try:
                    await self._segmented(client, remote, partial, sidecar, state)
                    offset = remote.size
                except _Mismatch as error:
                    self.note(f"{remote.path}: {error}; continuing as one stream from byte 0.")
                    log.info("range restart %s as one stream: %s", remote.path, error)
                    state = PartialState.for_remote(remote, version=1)
                    _replace(sidecar, _encode(state))
                    offset, hasher = await self._stream(
                        client, remote, partial, sidecar, state, 0, hashlib.sha256()
                    )
            else:
                offset = partial.stat().st_size
                hasher = await asyncio.to_thread(_hash_prefix, partial, offset)
                offset, hasher = await self._stream(
                    client, remote, partial, sidecar, state, offset, hasher
                )
            await asyncio.to_thread(_sync, partial)
            if hasher is None:
                # The segments wrote every byte; the file is hashed as a whole. The
                # progress bar reads 100% meanwhile, so the job log says what it is doing.
                self.note(f"{remote.path}: checking the sha256.")
                hasher = await asyncio.to_thread(_hash_prefix, partial, offset)
            digest = hasher.hexdigest()
            if offset != remote.size or digest != remote.etag:
                remove_partial(partial)
                log.warning(
                    "range hash mismatch %s: %s != %s, partial deleted",
                    remote.path,
                    digest,
                    remote.etag,
                )
                raise Fallback(
                    f"the downloaded bytes of {remote.path} did not match the Hub's sha256; "
                    "the partial file was deleted"
                )
            partial.chmod(0o666 & ~_umask())
            partial.replace(blob)
            sidecar.unlink(missing_ok=True)
            link_snapshot(folder, remote)
            log.info("range done %s sha256 %s ok, %d bytes", remote.path, digest, remote.size)
            return blob

    def _plan(self, remote: Remote, partial: Path, sidecar: Path) -> PartialState:
        """What this run continues. A sidecar that matches the Hub's file, with the
        partial it describes, continues as it was: version 2 from each segment's
        `verified_end`, version 1 from the partial's size. Anything else starts again
        as version 2, with the partial preallocated and the sidecar written here."""
        state = _read_state(sidecar)
        if state is not None and state.matches(remote) and partial.is_file():
            size = partial.stat().st_size
            if state.version == 1 and size <= remote.size:
                return state
            if state.version == 2 and size == remote.size and _covers(state, remote.size):
                return state
        if partial.is_file():
            log.info("range restart %s: the partial does not match the Hub", remote.path)
            self.note(
                f"{remote.path}: the partial file does not match the Hub's file; starting it again."
            )
        state = PartialState.for_remote(remote, version=2, segments=_plan_segments(remote.size))
        _preallocate(partial, remote.size)
        _replace(sidecar, _encode(state))
        return state

    async def _segmented(
        self,
        client: httpx.AsyncClient,
        remote: Remote,
        partial: Path,
        sidecar: Path,
        state: PartialState,
    ) -> None:
        """Fetch every segment that is not verified yet, all at once. Raises `_Mismatch`
        when the server does not answer as the plan needs, `Fallback` on any other
        problem. Cancellation keeps each segment's `verified_end` in the sidecar."""
        done = sum(segment.verified_end - segment.start for segment in state.segments)
        todo = [i for i, segment in enumerate(state.segments) if segment.verified_end < segment.end]
        if not todo:
            return
        streams = len(state.segments)
        self.note(
            f"{remote.path}: resuming in {streams} ranges from {done} bytes."
            if done
            else f"{remote.path}: downloading in {streams} ranges."
        )
        journal = _Journal(partial, sidecar, state)
        try:
            link = _Link(remote.location)
            await _join(
                [
                    asyncio.create_task(self._segment(client, remote, link, journal, index))
                    for index in todo
                ]
            )
        finally:
            journal.close()

    async def _segment(
        self,
        client: httpx.AsyncClient,
        remote: Remote,
        link: _Link,
        journal: _Journal,
        index: int,
    ) -> None:
        """One Range stream: from the segment's `verified_end` to its end, with the same
        retries as a single stream."""
        state = journal.state
        segment = state.segments[index]
        failures = 0
        while segment.verified_end < segment.end:
            before = segment.verified_end
            headers = self._headers(link.url)
            headers["Range"] = f"bytes={segment.verified_end}-{segment.end - 1}"
            if state.validator:
                headers["If-Range"] = f'"{state.validator}"'
            try:
                async with client.stream("GET", link.url, headers=headers) as response:
                    status = response.status_code
                    log.info(
                        "range GET %s Range: %s -> %s Content-Range: %s",
                        redact_url(link.url),
                        headers["Range"],
                        status,
                        response.headers.get("content-range", "-"),
                    )
                    if status in (401, 403, 410) and "Authorization" not in headers:
                        # A signed CDN URL expired (xet-bridge URLs last an hour).
                        raise _Expired(status)
                    if status == 200:
                        raise _Mismatch("the server ignored Range")
                    if status != 206:
                        raise Fallback(f"the download server answered {status}")
                    last = _check_range(response.headers.get("content-range"), segment, remote.size)
                    validator = _etag(response.headers.get("etag"))
                    if validator and validator != state.validator:
                        if state.validator is not None:
                            raise _Mismatch("the file changed while it was downloading")
                        state.validator = validator
                        journal.checkpoint()
                    async for chunk in response.aiter_bytes():
                        if segment.verified_end + len(chunk) > last + 1:
                            raise _Mismatch("the server sent more bytes than its Content-Range")
                        journal.write(segment.verified_end, chunk)
                        segment.verified_end += len(chunk)
                        journal.checkpoint()
                if segment.verified_end == before:
                    raise httpx.RemoteProtocolError("the server sent no bytes for the range")
                failures = 0
            except (_Expired, httpx.TransportError, httpx.DecodingError) as error:
                failures += 1
                reason = self._clean(str(error) or type(error).__name__)
                log.warning(
                    "range retry %s at byte %d: %s", remote.path, segment.verified_end, reason
                )
                if failures > RETRIES:
                    raise Fallback(f"{remote.path}: {reason}") from None
                await asyncio.sleep(min(30.0, RETRY_DELAY_S * 2 ** (failures - 1)))
                link.url = await self._fresh_location(client, remote, link.url)

    async def _stream(
        self,
        client: httpx.AsyncClient,
        remote: Remote,
        partial: Path,
        sidecar: Path,
        state: PartialState,
        offset: int,
        hasher: Any,
    ) -> tuple[int, Any]:
        """Append the rest of the file to the partial (version 1); returns the bytes
        written in all and the hasher (a new one if the server ignored Range and the
        file restarted)."""
        location, failures = remote.location, 0
        first = True
        with partial.open("r+b") as out:
            out.truncate(offset)
            out.seek(offset)
            while offset < remote.size:
                headers = self._headers(location)
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                    if state.validator:
                        headers["If-Range"] = f'"{state.validator}"'
                try:
                    async with client.stream("GET", location, headers=headers) as response:
                        status = response.status_code
                        log.info(
                            "range GET %s Range: %s -> %s Content-Range: %s",
                            redact_url(location),
                            headers.get("Range", "-"),
                            status,
                            response.headers.get("content-range", "-"),
                        )
                        if first:
                            first = False
                            self.note(
                                f"{remote.path}: resuming at byte {offset} "
                                f"(Range: bytes={offset}-) -> {status}"
                                if offset
                                else f"{remote.path}: downloading {remote.size} bytes -> {status}"
                            )
                        if status in (401, 403, 410) and "Authorization" not in headers:
                            # A signed CDN URL expired (xet-bridge URLs last an hour).
                            raise _Expired(status)
                        if status == 200 and offset:
                            log.info("range restart %s: Range was ignored", remote.path)
                            self.note(f"{remote.path}: the server ignored Range; starting again.")
                            offset = 0
                            out.truncate(0)
                            out.seek(0)
                            hasher = hashlib.sha256()
                        elif status == 206:
                            match = _CONTENT_RANGE.match(response.headers.get("content-range", ""))
                            if (
                                not match
                                or int(match.group(1)) != offset
                                or (match.group(3) != "*" and int(match.group(3)) != remote.size)
                            ):
                                raise Fallback(
                                    "the download server answered with an unexpected "
                                    f"Content-Range {response.headers.get('content-range')!r}"
                                )
                        elif status != 200:
                            raise Fallback(f"the download server answered {status}")
                        validator = _etag(response.headers.get("etag"))
                        if validator and validator != state.validator:
                            state.validator = validator
                            _replace(sidecar, _encode(state))
                        async for chunk in response.aiter_bytes():
                            if offset + len(chunk) > remote.size:
                                raise Fallback(f"the server sent more than {remote.size} bytes")
                            out.write(chunk)
                            hasher.update(chunk)
                            offset += len(chunk)
                    failures = 0
                except (_Expired, httpx.TransportError, httpx.DecodingError) as error:
                    failures += 1
                    reason = self._clean(str(error) or type(error).__name__)
                    log.warning("range retry %s at byte %d: %s", remote.path, offset, reason)
                    if failures > RETRIES:
                        raise Fallback(f"{remote.path}: {reason}") from None
                    out.flush()
                    await asyncio.sleep(min(30.0, RETRY_DELAY_S * 2 ** (failures - 1)))
                    location = await self._fresh_location(client, remote, location)
            out.flush()
        return offset, hasher

    async def _fresh_location(self, client: httpx.AsyncClient, remote: Remote, old: str) -> str:
        """A new signed URL for the same bytes; the old one when the Hub can't be asked."""
        try:
            again = await self.resolve(client, remote.repo, remote.revision, remote.path)
        except Fallback:
            return old
        if not (again.etag == remote.etag and again.size == remote.size):
            raise Fallback(f"{remote.path} changed on the Hub while it was downloading")
        return again.location

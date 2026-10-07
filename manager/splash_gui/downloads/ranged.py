"""Byte-level resumable downloads of large Hub files (SPEC §9.4, D61; Q24 option c).

Splash's installer (`install/models.py prepare`, huggingface_hub 1.28) starts a file
it did not finish from byte 0 in a new `<etag>.<uuid8>.incomplete`. For LFS/Xet files
of `RANGE_MIN_BYTES` or more, the Downloader therefore fetches the bytes itself with
HTTP Range requests into the same Hub cache first, and then runs `prepare` as before;
`prepare` finds the blob and its snapshot pointer and fetches only the small files.

Data shape, per file (next to the blob it becomes, in `models--O--R/blobs/`):

- **partial** `<etag>.splashgui.incomplete`: the file's first N bytes; its size N is
  the bytes done.
- **sidecar** `<etag>.splashgui.json`: `PartialState`, what those bytes belong to
  (repo, path, commit, etag = the sha256 that names the blob, expected size, Xet hash,
  the CDN's validator).
- **lock** `../.locks/models--O--R/<etag>.lock`: `flock`, the lock huggingface_hub
  holds while it writes that blob.

How the Downloader's job maps onto it (`downloads/service.py`):

- **Progress and speed** are the partial's size (`hubcache.blob_partials`), sampled
  every 500 ms like Splash's own partials.
- **Pause** cancels the job's task: the stream stops, the lock is released, the
  partial and sidecar stay.
- **Resume**, and a manager restart (the queue comes back `queued`), run the job
  again: the file is resolved again, and a sidecar that matches the Hub's etag, size
  and Xet hash continues with `Range: bytes=N-`. Anything else starts at 0.
- **Cancel** deletes the partial and its sidecar (`hubcache.remove_partial`).
- **Done**: the sha256 of every byte is checked before the atomic rename to
  `blobs/<etag>`; then the `snapshots/<commit>/<path>` link and `refs/<branch>` are
  written the way huggingface_hub writes them.
- **Any problem** (a hash mismatch, an unexpected status or `Content-Range`, missing
  metadata, a lock held for `LOCK_TIMEOUT_S`, repeated network errors) raises
  `Fallback`, and the job leaves that file to Splash's `prepare`: the worst case is
  the per-file behaviour Splash has anyway. A hash mismatch also deletes the partial.

The token goes only to the Hub's own host (`hf.endpoint`), never to the CDN that
`Location` points at; redirects are followed by hand. Logged URLs drop their query
string (a signed CDN URL is a credential for an hour) and the token is never logged.
Evidence: docs/progress/q24-range-prototype.md.
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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from ..hubcache import range_partial, range_sidecar, remove_partial, repo_folder

log = logging.getLogger(__name__)

# Files this large or larger are fetched with Range requests (D61). A fixed constant,
# not a setting: every supported GGUF and most MLX shards are above it, and the
# small files are cheap to restart.
RANGE_MIN_BYTES = 1_000_000_000
# How long to wait for another writer of the same blob (Splash's own installer, or a
# second download sharing a draft) before leaving the file to `prepare`, which waits on
# the same lock and then finds the blob.
LOCK_TIMEOUT_S = 60.0
# Network errors and expired CDN URLs retried in a row before falling back.
RETRIES = 5
RETRY_DELAY_S = 2.0
USER_AGENT = "splash-gui"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")


class Fallback(Exception):
    """This file is left to Splash's `prepare`; the message says why, in plain words."""


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
class PartialState:
    """The sidecar: what the bytes in the partial belong to."""

    repo: str
    path: str
    commit: str
    etag: str
    size: int
    xet_hash: str | None
    validator: str | None  # the CDN's ETag, sent back as If-Range

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


def _read_state(path: Path) -> PartialState | None:
    try:
        return PartialState(**json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
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


def link_snapshot(folder: Path, remote: Remote) -> None:
    """`snapshots/<commit>/<path>` → the relative path of `blobs/<etag>`, and
    `refs/<revision>` when the revision is a branch or tag (huggingface_hub
    `_create_symlink`, `_cache_commit_hash_for_specific_revision`)."""
    pointer = folder / "snapshots" / remote.commit / remote.path
    pointer.parent.mkdir(parents=True, exist_ok=True)
    target = os.path.relpath(folder / "blobs" / remote.etag, pointer.parent)
    if not (pointer.is_symlink() and str(pointer.readlink()) == target):
        pointer.unlink(missing_ok=True)
        pointer.symlink_to(target)
    if remote.revision != remote.commit:
        ref = folder / "refs" / remote.revision
        if not ref.is_file() or ref.read_text() != remote.commit:
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
            # Only the Hub itself gets the token, never the CDN (D61).
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def resolve(
        self, client: httpx.AsyncClient, repo: str, revision: str | None, path: str
    ) -> Remote:
        """HEAD the resolve URL, following only relative (same-Hub) redirects, as
        huggingface_hub's `_httpx_follow_relative_redirects_with_backoff` does, and read
        the headers its `get_hf_file_metadata` reads."""
        revision = revision or "main"
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
        """Fetch `remote` into `blobs/<etag>` (continuing a matching partial), then
        link it into the snapshot. Raises `Fallback`; cancellation keeps the partial."""
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
            state = _read_state(sidecar)
            offset = partial.stat().st_size if partial.is_file() else 0
            if offset and (state is None or not state.matches(remote) or offset > remote.size):
                log.info("range restart %s: the partial does not match the Hub", remote.path)
                self.note(
                    f"{remote.path}: the partial file does not match the Hub's file; "
                    "starting it again."
                )
                offset, state = 0, None
            if state is None:
                state = PartialState(
                    remote.repo,
                    remote.path,
                    remote.commit,
                    remote.etag,
                    remote.size,
                    remote.xet_hash,
                    None,
                )
                partial.write_bytes(b"")
                _replace(sidecar, json.dumps(asdict(state)).encode())
            # Hash what is on disk, then keep hashing the stream.
            hasher = await asyncio.to_thread(_hash_prefix, partial, offset)
            offset, hasher = await self._stream(
                client, remote, partial, sidecar, state, offset, hasher
            )
            await asyncio.to_thread(_sync, partial)
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
        """Append the rest of the file to the partial; returns the bytes written in all
        and the hasher (a new one if the server ignored Range and the file restarted)."""
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
                            _replace(sidecar, json.dumps(asdict(state)).encode())
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


class _Expired(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"the download URL answered {status} (expired)")

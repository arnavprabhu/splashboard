"""Persisted installer queue with pause/resume and byte progress (SPEC §9.4).

A job first fetches its large LFS/Xet files itself with byte-resumable Range requests
(D61, `ranged.py`, which documents the per-file data shape), then runs Splash's
`install/models.py prepare`, which finds those blobs and fetches the rest."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import time
import uuid
from collections.abc import Awaitable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, TypeVar

from ..errors import ApiError
from ..events.alerts import action
from ..hubcache import blob_partials, blobs_dir, range_partial, remove_partial, repo_folder
from ..models.hf import HubError, is_blob_id
from ..models.layout import read_all
from ..models.service import valid_id
from ..paths import splash_models_dir, write_atomic
from ..schemas import DownloadError, DownloadFile, DownloadItem, DownloadRequest, ModelsChangedEvent
from ..settings.parsers import split_model_id
from ..units import format_bytes
from ..usage.db import iso
from .ranged import Fallback, RangeDownloader, Remote, is_candidate, partial_progress

if TYPE_CHECKING:
    from ..state import ManagerState

log = logging.getLogger(__name__)
T = TypeVar("T")

# Fetches a draft repo's JSON and safetensors into HF_HUB_CACHE at the commit argv[2]
# (argv[1] is the repo), then names the commit in `refs/main`, which the local drop-in
# reads as "complete" (`LocalModels._draft_complete`). snapshot_download names a branch
# only when it is given one, so the ref is written here.
DRAFT_SCRIPT = """\
import os, sys
from huggingface_hub import snapshot_download
repo, commit = sys.argv[1], sys.argv[2]
path = snapshot_download(repo, revision=commit or None, allow_patterns=["*.json", "*.safetensors"])
if commit:
    refs = os.path.join(os.path.dirname(os.path.dirname(path)), "refs")
    os.makedirs(refs, exist_ok=True)
    with open(os.path.join(refs, "main"), "w") as handle:
        handle.write(commit)
"""


def _load_blob_map(raw: object) -> dict[str, dict[str, str]]:
    """The persisted blob map, `{download id: {repo/file: blob ID}}` (SPEC §9.4). Cancel
    joins each blob ID into the blobs folder and unlinks what it finds there, so a value
    that is not a Hub blob ID (`../../x` reaches the models folder) is dropped and logged.
    Its file then has no blob, and nothing is joined or removed for it."""
    if not isinstance(raw, dict):
        log.warning("downloads.json: the blob map is not an object; ignored")
        return {}
    blobs: dict[str, dict[str, str]] = {}
    for download, mapping in raw.items():
        if not isinstance(mapping, dict):
            log.warning("downloads.json: the blobs of %s are not an object; ignored", download)
            continue
        kept: dict[str, str] = {}
        for key, digest in mapping.items():
            if is_blob_id(digest):
                kept[key] = digest
            else:
                log.warning(
                    "downloads.json: dropped %s of %s, %r is not a Hub blob ID",
                    key,
                    download,
                    digest,
                )
        blobs[download] = kept
    return blobs


class Downloads:
    def __init__(self, state: ManagerState) -> None:
        self.state = state
        self.items: dict[str, DownloadItem] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.blobs: dict[str, dict[str, str]] = {}
        self.stopping = False
        self.queue_lock = asyncio.Lock()
        self.preexisting: dict[str, list[str]] = {}

    async def start(self) -> None:
        try:
            raw = json.loads(self.state.paths.downloads_file.read_text())
            self.blobs = _load_blob_map(raw.get("blobs", {}))
            self.preexisting = raw.get("preexisting", {})
            for data in raw["items"]:
                item = DownloadItem.model_validate(data)
                if item.state in ("running", "verifying"):
                    item.state = "queued"
                for file in item.files:
                    # A file whose blob ID was dropped has no Range path (fetch_large skips it).
                    key = f"{file.repo_id}/{file.name}"
                    if file.resumable and key not in self.blobs.get(item.id, {}):
                        file.resumable = False
                self.items[item.id] = item
        except FileNotFoundError:
            pass
        self.schedule()

    def save(self) -> None:
        write_atomic(
            self.state.paths.downloads_file,
            json.dumps(
                {
                    "items": [i.model_dump() for i in self.items.values()],
                    "blobs": self.blobs,
                    "preexisting": self.preexisting,
                }
            ).encode(),
        )

    def publish(self, item: DownloadItem) -> None:
        self.state.events.publish("download.progress", item)
        self.save()

    def active_model(self, model: str) -> bool:
        return any(
            i.model == model and i.state in ("queued", "running", "verifying", "paused")
            for i in self.items.values()
        )

    def get(self, dl: str) -> DownloadItem:
        if dl not in self.items:
            raise ApiError(404, "No such download", "download_not_found")
        return self.items[dl]

    async def queue(self, body: DownloadRequest) -> DownloadItem:
        async with self.queue_lock:
            return await self._queue(body)

    async def queue_draft(self, repo_id: str) -> DownloadItem:
        """The local drop-in's DFlash2 draft (SPEC §9.6, D68): an ordinary queue item of
        kind `draft`, which fetches the draft repo's JSON and safetensors only."""
        async with self.queue_lock:
            return await self._queue(DownloadRequest(id=repo_id, verify=False), draft=True)

    async def _queue(self, body: DownloadRequest, *, draft: bool = False) -> DownloadItem:
        if self.state.jobs.running("storage_move") or self.state.jobs.running("import"):
            raise ApiError(409, "Wait for the storage operation to finish", "storage_busy")
        valid_id(body.id)
        if self.active_model(body.id):
            raise ApiError(409, "This model is already in the queue", "download_exists")
        item = DownloadItem(
            id=uuid.uuid4().hex,
            model=body.id,
            kind="draft" if draft else "model",
            revision=body.revision,
            draft_model=body.draft_model,
            language_only=body.language_only,
            verify=body.verify,
            state="queued",
            created_at=iso(),
        )
        repos = [(body.id, None, [])] if draft else await self._model_repos(body)
        blob_map: dict[str, str] = {}
        try:
            for repo_id, revision, selected in repos:
                repo = await self.state.models.hf.repo_info(repo_id, revision)
                if draft and repo.sha:
                    # The draft is fetched at the commit listed here, so its blobs match.
                    item.revision = repo.sha
                for name, size in repo.files.items():
                    if selected and name not in selected:
                        continue
                    if not selected and not (name.endswith((".json", ".safetensors"))):
                        continue
                    key = repo_id + "/" + name
                    if name in repo.blobs:
                        blob_map[key] = repo.blobs[name]
                    item.files.append(
                        DownloadFile(
                            name=name,
                            repo_id=repo_id,
                            size_bytes=size,
                            resumable=is_candidate(repo.blobs.get(name), size),
                        )
                    )
        except HubError as error:
            raise ApiError(error.status, error.message, "hub_unreachable") from None
        item.bytes_total = sum(f.size_bytes or 0 for f in item.files) or None
        self.blobs[item.id] = blob_map
        self.progress(item)
        directory = self.state.settings.models_dir()
        directory.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(directory).free
        required = (item.bytes_total or 0) - item.bytes_done + 2 * 1024**3
        if free < required:
            # Not queued: forget its blob map too, or every later cancel would look up
            # an item that does not exist.
            self.blobs.pop(item.id, None)
            raise ApiError(
                507,
                f"Not enough disk space: need {format_bytes(required)}, have {format_bytes(free)}",
                "disk_full",
            )
        # What the job found on disk: partial blobs, and complete blobs it must not
        # delete on cancel (SPEC §9.4). A snapshot link to a complete blob is never
        # created by this job, so the blobs are enough to protect the links too.
        complete = [
            str(self.blob_path(file.repo_id, digest))
            for file in item.files
            if (digest := blob_map.get(file.repo_id + "/" + file.name))
            and self.blob_path(file.repo_id, digest).is_file()
        ]
        self.preexisting[item.id] = [
            str(p) for p in directory.glob("models--*/blobs/*.incomplete")
        ] + complete
        self.items[item.id] = item
        self.publish(item)
        self.schedule()
        return item

    async def _model_repos(self, body: DownloadRequest) -> list[tuple[str, str | None, list[str]]]:
        """The repositories a model download fetches, with the files it selects in each.
        Inspection also resolves the exact file set, before any weight is downloaded."""
        inspection = await self.state.models.inspect(body.id, revision=body.revision)
        if not inspection.compatible:
            message = inspection.reason or "Model is incompatible"
            if inspection.reason_detail:
                # D53: the plain line first, Splash's own words after it.
                message += f" Splash's check: {inspection.reason_detail}"
            raise ApiError(
                422,
                message,
                "incompatible",
                details={"reason_detail": inspection.reason_detail}
                if inspection.reason_detail
                else None,
            )
        if (
            not body.language_only
            and not inspection.vision.available
            and inspection.format != "legacy"
        ):
            raise ApiError(
                422,
                inspection.vision.reason or "Use language-only for this model",
                "vision_unavailable",
            )
        repos: list[tuple[str, str | None, list[str]]] = [
            (
                inspection.repo_id,
                body.revision,
                (
                    self.state.models.language_file_sets
                    if body.language_only
                    else self.state.models.file_sets
                ).get(body.id, []),
            )
        ]
        if body.draft_model or inspection.draft:
            draft = body.draft_model or inspection.draft
            if draft and not Path(draft).is_absolute():
                repos.append((draft, None, []))
        return repos

    def schedule(self) -> None:
        if self.stopping:
            return
        parallel = self.state.settings.current.global_.downloads.parallel
        for item in self.items.values():
            if len(self.tasks) >= parallel:
                break
            if item.state == "queued" and item.id not in self.tasks:
                task = asyncio.create_task(self.run(item))
                self.tasks[item.id] = task
                task.add_done_callback(partial(self.finished, item.id))

    def finished(self, dl: str, task: asyncio.Task[None]) -> None:
        self.tasks.pop(dl, None)
        self.schedule()

    def blob_path(self, repo_id: str, digest: str) -> Path:
        return blobs_dir(self.state.settings.models_dir(), repo_id) / digest

    def partials(self, repo_id: str, digest: str) -> list[Path]:
        """A blob's partial files. huggingface_hub before 1.x used `<etag>.incomplete`;
        1.28 (bundled with Splash 1.2.0) writes a per-process `<etag>.<uuid8>.incomplete`
        (huggingface_hub/file_download.py `_download_to_tmp_and_move`, PR #4228)."""
        return blob_partials(self.blob_path(repo_id, digest))

    def progress(self, item: DownloadItem) -> None:
        for file in item.files:
            digest = self.blobs.get(item.id, {}).get(file.repo_id + "/" + file.name)
            if not digest:
                continue
            path = self.blob_path(file.repo_id, digest)
            try:
                if path.is_file():
                    file.done_bytes, file.state = path.stat().st_size, "done"
                    continue
            except FileNotFoundError:
                pass
            # The file being written is the newest partial; older ones are left over
            # from a paused or killed run (the hub cannot continue them).
            sizes: list[tuple[float, int]] = []
            for partial_path in self.partials(file.repo_id, digest):
                with contextlib.suppress(FileNotFoundError):
                    info = partial_path.stat()
                    # A Range partial is preallocated (D84): its verified bytes are the progress.
                    sizes.append((info.st_mtime, partial_progress(partial_path)))
            if sizes:
                file.done_bytes, file.state = max(sizes)[1], "downloading"
        item.bytes_done = sum(f.done_bytes for f in item.files)
        item.progress = min(1.0, item.bytes_done / item.bytes_total) if item.bytes_total else None

    def shared_digests(self, dl: str) -> set[str]:
        """Blobs another unfinished download also needs: their partials are not ours to delete."""
        return self.active_digests(exclude=dl)

    def active_digests(self, exclude: str | None = None) -> set[str]:
        """Blobs every unfinished download (queued, running, verifying, paused) needs.
        The engine supervisor's stale-partial cleanup leaves their partials alone."""
        return {
            blob
            for key, mapping in self.blobs.items()
            if key != exclude
            and key in self.items
            and self.items[key].state not in ("cancelled", "done", "failed")
            for blob in mapping.values()
        }

    def remove_partials(self, item: DownloadItem, *, stale_only: bool) -> None:
        """Delete this download's partial files, never one that existed before it was
        queued or one another download shares. `stale_only` keeps `<etag>.incomplete`,
        which an older hub could still continue, and removes only the per-process
        `<etag>.<uuid>.incomplete` files that no later run can reuse."""
        shared = self.shared_digests(item.id)
        keep = set(self.preexisting.get(item.id, []))
        for file in item.files:
            digest = self.blobs.get(item.id, {}).get(file.repo_id + "/" + file.name)
            if not digest or digest in shared:
                continue
            blob = self.blob_path(file.repo_id, digest)
            # Kept on resume: what an older hub (`<etag>.incomplete`) or the Range
            # download (`<etag>.splashgui.incomplete`, D61) continues.
            resumable = {blob.name + ".incomplete", range_partial(blob).name}
            for path in self.partials(file.repo_id, digest):
                if str(path) in keep or (stale_only and path.name in resumable):
                    continue
                remove_partial(path)

    def remove_finished_range_partials(self, item: DownloadItem) -> None:
        """A Range partial whose blob exists (the file fell back to Splash's installer,
        which finished it) can never be continued."""
        for file in item.files:
            digest = self.blobs.get(item.id, {}).get(file.repo_id + "/" + file.name)
            if digest and self.blob_path(file.repo_id, digest).is_file():
                remove_partial(range_partial(self.blob_path(file.repo_id, digest)))

    def note(self, item: DownloadItem, line: str) -> None:
        token = self.state.models.hf.token()
        if token:
            line = line.replace(token, "[redacted]")
        item.log_tail = [*item.log_tail, line][-200:]

    async def watch(self, item: DownloadItem, work: Awaitable[T]) -> T:
        """Run `work`, sampling byte progress, speed and ETA every 500 ms meanwhile.
        Cancelling the caller cancels `work`."""
        task = asyncio.ensure_future(work)
        previous, tick = item.bytes_done, time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=0.5)
                self.progress(item)
                now = time.monotonic()
                speed = max(0, item.bytes_done - previous) / max(0.001, now - tick)
                item.speed_bps = (
                    speed if item.speed_bps is None else item.speed_bps * 0.9 + speed * 0.1
                )
                item.eta_s = (
                    max(0, (item.bytes_total - item.bytes_done) / item.speed_bps)
                    if item.bytes_total and item.speed_bps
                    else None
                )
                previous, tick = item.bytes_done, now
                self.publish(item)
                if done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def fetch_large(self, item: DownloadItem) -> None:
        """D61: fetch the job's large LFS/Xet files with byte-resumable Range requests
        before `prepare`. A file that hits any problem is left to `prepare`."""
        if self.state.settings.current.global_.hf.offline:
            return
        mapping = self.blobs.setdefault(item.id, {})
        target = split_model_id(item.model)[0]
        todo = [
            f
            for f in item.files
            if is_candidate(mapping.get(f.repo_id + "/" + f.name), f.size_bytes)
            and not self.blob_path(f.repo_id, mapping[f.repo_id + "/" + f.name]).is_file()
        ]
        if not todo:
            return
        hf = self.state.models.hf
        downloader = RangeDownloader(
            self.state.settings.models_dir(),
            hf.endpoint,
            hf.token(),
            note=lambda line: self.note(item, line),
        )
        async with downloader.client() as client:
            for file in todo:
                key = file.repo_id + "/" + file.name
                file.resumable = True
                revision = item.revision if file.repo_id == target else None
                try:
                    remote = await downloader.resolve(client, file.repo_id, revision, file.name)
                    if remote.etag != mapping[key]:
                        self.retarget(item, file, key, remote)
                    self.publish(item)
                    await self.watch(item, downloader.download(client, remote))
                except Fallback as error:
                    file.resumable = False
                    log.warning("range fallback for %s: %s", key, error)
                    self.note(
                        item, f"{file.name}: {error}. Splash's installer downloads it instead."
                    )
                    self.publish(item)

    def retarget(self, item: DownloadItem, file: DownloadFile, key: str, remote: Remote) -> None:
        """The file changed on the Hub since the job was queued (a new etag): its bytes
        so far belong to the old version, so they go and the new one starts at 0."""
        mapping = self.blobs[item.id]
        old = mapping[key]
        if old not in self.shared_digests(item.id):
            keep = set(self.preexisting.get(item.id, []))
            stale = range_partial(self.blob_path(file.repo_id, old))
            if str(stale) not in keep:
                remove_partial(stale)
        mapping[key] = remote.etag
        file.size_bytes = remote.size
        file.done_bytes, file.state = 0, "pending"
        item.bytes_total = sum(f.size_bytes or 0 for f in item.files) or None
        log.info("range %s changed on the Hub: %s -> %s", key, old, remote.etag)
        self.note(item, f"{file.name} changed on the Hub; downloading the new version from 0.")

    def job_argv(self, item: DownloadItem, action: str) -> tuple[list[str], dict[str, str]]:
        """The process a job runs. A draft (D68) is fetched with `huggingface_hub` under the
        engine's own Python: Splash's installer prepares only a model with its draft, so it
        cannot fetch a draft alone. The process is a plain child, so Pause signals it like
        the installer."""
        if item.kind == "draft":
            engine = self.state.engine_cached()
            if not engine.python:
                raise ApiError(503, "Install Splash before managing models", "engine_unavailable")
            argv = [str(engine.python), "-c", DRAFT_SCRIPT, item.model, item.revision or ""]
            return argv, self.state.models.env()
        argv, env = self.state.models.installer(
            item.model,
            action,
            revision=item.revision,
            draft_model=item.draft_model,
            language_only=item.language_only,
            full=action == "verify" and self.state.settings.current.global_.downloads.full_verify,
        )
        return argv, env

    async def command(self, item: DownloadItem, action: str) -> None:
        argv, env = self.job_argv(item, action)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self.processes[item.id] = proc

        async def read() -> None:
            assert proc.stdout
            async for line in proc.stdout:
                text = line.decode(errors="replace").rstrip()
                token = self.state.models.hf.token()
                if token:
                    text = text.replace(token, "[redacted]")
                item.log_tail = ([*item.log_tail, text])[-200:]

        reader = asyncio.create_task(read())
        try:
            await self.watch(item, proc.wait())
            await reader
            if proc.returncode or any(
                "Warning: keeping the installed" in line for line in item.log_tail
            ):
                raise RuntimeError(
                    "\n".join(item.log_tail[-8:]) or f"Installer exited {proc.returncode}"
                )
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            self.processes.pop(item.id, None)

    async def run(self, item: DownloadItem) -> None:
        # Splash's installer starts each unfinished file again in a new partial file;
        # the ones a paused run left behind would only waste disk (SPEC §9.4). The
        # Range partials (D61) are kept: this run continues them.
        self.remove_partials(item, stale_only=True)
        self.progress(item)
        item.state, item.started_at, item.error = "running", iso(), None
        self.publish(item)
        try:
            await self.fetch_large(item)
            await self.command(item, "prepare")
            self.remove_finished_range_partials(item)
            if item.verify:
                item.state = "verifying"
                self.publish(item)
                await self.command(item, "verify")
            item.state, item.finished_at, item.progress = "done", iso(), 1.0
            if item.kind == "draft":
                # The local drop-in polls the cache and registers its file on its next pass.
                return
            self.state.alerts.raise_alert(
                "download_done",
                f"{item.model} downloaded",
                "Load now",
                source="downloader",
                subject=item.id,
                actions=[
                    action("load", "Load now", "/api/admin/engine/load", body={"model": item.model})
                ],
            )
            self.state.events.publish(
                "models.changed", ModelsChangedEvent(reason="downloaded", model=item.model)
            )
        except asyncio.CancelledError:
            if item.state not in ("paused", "cancelled"):
                item.state = "queued"
            raise
        except Exception as error:
            message = str(error)
            item.error = self.classify(item, message)
            item.state = "failed"
            self.state.alerts.raise_alert(
                "download_failed",
                "Download failed",
                message,
                source="downloader",
                subject=item.id,
            )
        finally:
            if item.state != "done":
                # Paused or failed: show the bytes on disk now, not the last 500 ms sample
                # (the Range partial's size is where Resume continues).
                self.progress(item)
            self.publish(item)

    # SPEC §9.4 plain-language errors: each code carries the action the UI offers.
    ACTIONS: ClassVar[dict[str, str | None]] = {
        "gated": "add_hf_token",
        "disk_full": "free_space",
        "hub_unreachable": "retry",
        "installer_failed": "retry",
        "verify_failed": "retry",
        "incompatible": None,
    }
    _UNREACHABLE = (
        "connection",
        "unreachable",
        "name resolution",
        "timed out",
        "network is",
        "offline",
    )

    def classify(self, item: DownloadItem, message: str) -> DownloadError:
        lower = message.lower()
        if any(x in message for x in ("401", "403")) or "gated" in lower:
            code = "gated"
        elif "space" in lower or "errno 28" in lower:
            code = "disk_full"
        elif "incompatible" in lower or "not supported" in lower:
            code = "incompatible"
        elif any(x in lower for x in self._UNREACHABLE):
            code = "hub_unreachable"
        else:
            code = "installer_failed"
        needed = free = None
        if code == "disk_full":
            needed = max(0, (item.bytes_total or 0) - item.bytes_done) + 2 * 1024**3
            with contextlib.suppress(OSError):
                free = shutil.disk_usage(self.state.settings.models_dir()).free
        return DownloadError.model_validate(
            {
                "code": code,
                "message": message,
                "action": self.ACTIONS.get(code, "retry"),
                "needed_bytes": needed,
                "free_bytes": free,
            }
        )

    async def pause(self, dl: str) -> DownloadItem:
        item = self.get(dl)
        if item.state not in ("queued", "running", "verifying"):
            raise ApiError(
                409, "Only queued or running downloads can be paused", "invalid_download_state"
            )
        item.state = "paused"
        task = self.tasks.get(dl)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.publish(item)
        return item

    def resume(self, dl: str) -> DownloadItem:
        item = self.get(dl)
        if item.state not in ("paused", "failed"):
            raise ApiError(
                409, "Only paused or failed downloads can be resumed", "invalid_download_state"
            )
        item.state, item.error = "queued", None
        self.publish(item)
        self.schedule()
        return item

    def remove_downloaded(self, item: DownloadItem) -> None:
        """SPEC §9.4: a cancelled download also removes the snapshot links and blobs it
        created, unless an installed model or another unfinished download uses them.
        A blob that was complete when the job was queued is never removed, and a job
        with no record of what it found (queued before this record existed) removes
        nothing. A blob is removed only when every snapshot link to it is one of this
        job's own files."""
        if item.id not in self.preexisting:
            return
        keep = set(self.preexisting[item.id])
        shared = self.shared_digests(item.id)
        models_dir = self.state.settings.models_dir()
        installed = {
            path.resolve()
            for selection in read_all(splash_models_dir())
            for path in selection.real_paths()
        }
        # Every snapshot link in the cache, by the blob it resolves to.
        links: dict[Path, list[Path]] = {}
        for folder in models_dir.glob("models--*"):
            for link in (folder / "snapshots").rglob("*"):
                if link.is_symlink():
                    links.setdefault(link.resolve(), []).append(link)
        for file in item.files:
            digest = self.blobs.get(item.id, {}).get(file.repo_id + "/" + file.name)
            if not digest or digest in shared:
                continue
            blob = self.blob_path(file.repo_id, digest)
            if str(blob) in keep or not blob.is_file() or blob.resolve() in installed:
                continue
            snapshots = repo_folder(models_dir, file.repo_id) / "snapshots"
            names = [Path(f.name).parts for f in item.files if f.repo_id == file.repo_id]
            found = links.get(blob.resolve(), [])
            if not all(
                link.is_relative_to(snapshots)
                and any(link.parts[-len(name) :] == name for name in names)
                for link in found
            ):
                continue  # another snapshot, of this job or any other, still uses it
            for link in found:
                link.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    link.parent.rmdir()  # a snapshot folder only when it is empty
            blob.unlink(missing_ok=True)

    async def cancel(self, dl: str, keep_files: bool) -> None:
        item = self.get(dl)
        if item.state in ("queued", "running", "verifying"):
            await self.pause(dl)
        item.state = "cancelled"
        if not keep_files:
            self.remove_partials(item, stale_only=False)
            self.remove_downloaded(item)
        self.publish(item)

    async def shutdown(self) -> None:
        self.stopping = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.save()


def create(state: ManagerState) -> Downloads:
    service = Downloads(state)
    state.downloads = service
    return service

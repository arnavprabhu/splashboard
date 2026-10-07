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
from ..hubcache import blob_partials, blobs_dir, range_partial, remove_partial
from ..models.hf import HubError
from ..models.service import valid_id
from ..paths import write_atomic
from ..schemas import DownloadError, DownloadFile, DownloadItem, DownloadRequest, ModelsChangedEvent
from ..settings.parsers import split_model_id
from ..units import format_bytes
from ..usage.db import iso
from .ranged import Fallback, RangeDownloader, Remote, is_candidate

if TYPE_CHECKING:
    from ..state import ManagerState

log = logging.getLogger(__name__)
T = TypeVar("T")


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
            self.blobs = raw.get("blobs", {})
            self.preexisting = raw.get("preexisting", {})
            for data in raw["items"]:
                item = DownloadItem.model_validate(data)
                if item.state in ("running", "verifying"):
                    item.state = "queued"
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

    async def _queue(self, body: DownloadRequest) -> DownloadItem:
        if self.state.jobs.running("storage_move") or self.state.jobs.running("import"):
            raise ApiError(409, "Wait for the storage operation to finish", "storage_busy")
        valid_id(body.id)
        if self.active_model(body.id):
            raise ApiError(409, "This model is already in the queue", "download_exists")
        item = DownloadItem(
            id=uuid.uuid4().hex,
            model=body.id,
            revision=body.revision,
            draft_model=body.draft_model,
            language_only=body.language_only,
            verify=body.verify,
            state="queued",
            created_at=iso(),
        )
        # Inspection also resolves the exact file set, before any weight is downloaded.
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
        repos = [
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
        blob_map: dict[str, str] = {}
        try:
            for repo_id, revision, selected in repos:
                repo = await self.state.models.hf.repo_info(repo_id, revision)
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
        self.preexisting[item.id] = [str(p) for p in directory.glob("models--*/blobs/*.incomplete")]
        self.items[item.id] = item
        self.publish(item)
        self.schedule()
        return item

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
                    sizes.append((info.st_mtime, info.st_size))
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

    async def command(self, item: DownloadItem, action: str) -> None:
        argv, env = self.state.models.installer(
            item.model,
            action,
            revision=item.revision,
            draft_model=item.draft_model,
            language_only=item.language_only,
            full=action == "verify" and self.state.settings.current.global_.downloads.full_verify,
        )
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

    async def cancel(self, dl: str, keep_files: bool) -> None:
        item = self.get(dl)
        if item.state in ("queued", "running", "verifying"):
            await self.pause(dl)
        item.state = "cancelled"
        if not keep_files:
            self.remove_partials(item, stale_only=False)
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

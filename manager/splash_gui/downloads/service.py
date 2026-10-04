"""Persisted installer queue with pause/resume and byte progress (SPEC §9.4)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import time
import uuid
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from ..errors import ApiError
from ..events.alerts import action
from ..models.hf import HubError
from ..models.service import valid_id
from ..paths import write_atomic
from ..schemas import DownloadError, DownloadFile, DownloadItem, DownloadRequest, ModelsChangedEvent
from ..usage.db import iso

if TYPE_CHECKING:
    from ..state import ManagerState


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
            raise ApiError(422, inspection.reason or "Model is incompatible", "incompatible")
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
                    item.files.append(DownloadFile(name=name, repo_id=repo_id, size_bytes=size))
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
                507, f"Not enough disk space: need {required} bytes, have {free}", "disk_full"
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

    def progress(self, item: DownloadItem) -> None:
        for file in item.files:
            digest = self.blobs.get(item.id, {}).get(file.repo_id + "/" + file.name)
            if not digest:
                continue
            path = (
                self.state.settings.models_dir()
                / ("models--" + file.repo_id.replace("/", "--"))
                / "blobs"
                / digest
            )
            partial = path.with_name(path.name + ".incomplete")
            try:
                if path.is_file():
                    file.done_bytes, file.state = path.stat().st_size, "done"
                elif partial.is_file():
                    file.done_bytes, file.state = partial.stat().st_size, "downloading"
            except FileNotFoundError:
                pass
        item.bytes_done = sum(f.done_bytes for f in item.files)
        item.progress = min(1.0, item.bytes_done / item.bytes_total) if item.bytes_total else None

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
        previous, tick = item.bytes_done, time.monotonic()
        try:
            while proc.returncode is None:
                await asyncio.sleep(0.5)
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
        item.state, item.started_at, item.error = "running", iso(), None
        self.publish(item)
        try:
            await self.command(item, "prepare")
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
            shared = {
                blob
                for key, mapping in self.blobs.items()
                if key != dl
                and key in self.items
                and self.items[key].state not in ("cancelled", "done", "failed")
                for blob in mapping.values()
            }
            for file in item.files:
                digest = self.blobs.get(dl, {}).get(file.repo_id + "/" + file.name)
                if digest and digest not in shared:
                    path = (
                        self.state.settings.models_dir()
                        / ("models--" + file.repo_id.replace("/", "--"))
                        / "blobs"
                        / (digest + ".incomplete")
                    )
                    if str(path) not in self.preexisting.get(dl, []):
                        path.unlink(missing_ok=True)
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

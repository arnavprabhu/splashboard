"""Background jobs with progress on `/events` (`job` events, docs/api.md §4).

A job is an asyncio task; it reports output lines and progress through the
`Job` handle it receives, and ends as `done` or `failed`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from .events.bus import EventBus
from .schemas import JobAccepted, JobEvent

log = logging.getLogger(__name__)

JobKind = Literal["verify", "storage_move", "import", "engine_install", "engine_upgrade"]
JobState = Literal["running", "done", "failed"]


@dataclass
class Job:
    id: str
    kind: JobKind
    bus: EventBus
    model: str | None = None
    state: JobState = "running"
    progress: float | None = None
    message: str | None = None
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=500))
    task: asyncio.Task[None] | None = None

    def _emit(self, line: str | None = None) -> None:
        self.bus.publish(
            "job",
            JobEvent(
                job_id=self.id,
                kind=self.kind,
                state=self.state,
                model=self.model,
                progress=self.progress,
                message=self.message,
                line=line,
            ),
        )

    def line(self, text: str) -> None:
        self.lines.append(text)
        self._emit(text)

    def update(self, *, progress: float | None = None, message: str | None = None) -> None:
        if progress is not None:
            self.progress = max(0.0, min(1.0, progress))
        if message is not None:
            self.message = message
        self._emit()

    def finish(self, ok: bool, message: str | None = None) -> None:
        self.state = "done" if ok else "failed"
        if ok:
            self.progress = 1.0
        if message is not None:
            self.message = message
        self._emit()


class JobFailed(Exception):
    """Raised inside a job body to fail it with a user-facing message."""


@dataclass
class Jobs:
    bus: EventBus
    _jobs: dict[str, Job] = field(default_factory=dict)

    def start(
        self,
        kind: JobKind,
        body: Callable[[Job], Awaitable[None]],
        *,
        model: str | None = None,
    ) -> JobAccepted:
        job = Job(id=uuid.uuid4().hex, kind=kind, bus=self.bus, model=model)
        self._jobs[job.id] = job

        async def run() -> None:
            job._emit()
            try:
                await body(job)
            except JobFailed as error:
                job.finish(False, str(error))
            except asyncio.CancelledError:
                job.finish(False, "cancelled")
                raise
            except Exception as error:
                log.exception("job %s (%s) failed", job.id, kind)
                job.finish(False, str(error) or type(error).__name__)
            else:
                if job.state == "running":
                    job.finish(True)

        job.task = asyncio.get_running_loop().create_task(run())
        return JobAccepted(job_id=job.id, kind=kind)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def running(self, kind: JobKind, model: str | None = None) -> Job | None:
        for job in self._jobs.values():
            if (
                job.kind == kind
                and job.state == "running"
                and (model is None or job.model == model)
            ):
                return job
        return None

    async def shutdown(self) -> None:
        tasks = [j.task for j in self._jobs.values() if j.task and not j.task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task

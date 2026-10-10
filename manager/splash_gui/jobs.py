"""Background jobs with progress on `/events` (`job` events).

A job is an asyncio task; it reports output lines and progress through the
`Job` handle it receives, and ends as `done` or `failed`. Work that must not be
abandoned halfway (moving files and recording where they went) runs through
`Job.protect`, so cancelling the job, as shutdown does, waits for it to finish.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from .events.bus import EventBus
from .schemas import JobAccepted, JobEvent, JobView

log = logging.getLogger(__name__)

JobKind = Literal["verify", "storage_move", "import", "engine_install", "engine_upgrade"]
JobState = Literal["running", "done", "failed"]
T = TypeVar("T")


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
    protected: set[asyncio.Future[Any]] = field(default_factory=set)

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

    def view(self) -> JobView:
        return JobView(
            job_id=self.id,
            kind=self.kind,
            state=self.state,
            model=self.model,
            progress=self.progress,
            message=self.message,
            lines=list(self.lines),
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

    async def protect(self, work: Awaitable[T]) -> T:
        """Run `work` to the end even if the job is cancelled meanwhile.

        A worker thread can't be interrupted, so cancelling the code that awaits
        it would leave the thread changing files while nothing records the
        outcome. Here a cancellation waits for `work` to finish (or fail), and
        takes effect afterwards; an error from `work` wins over it.
        """
        future: asyncio.Future[T] = asyncio.ensure_future(work)
        self.protected.add(future)
        future.add_done_callback(self.protected.discard)
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(future)
            except asyncio.CancelledError:
                if future.done():
                    raise
                cancelled = True
                continue
            if cancelled:
                raise asyncio.CancelledError
            return result

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
                # A protected step may already have finished the job for real.
                if job.state == "running":
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

    def all(self) -> list[Job]:
        return list(self._jobs.values())

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
        """Cancel running jobs and wait for them. A job inside a protected step
        (a storage move, say) finishes that step first, however long it takes:
        stopping halfway would leave files and settings disagreeing."""
        busy = [j for j in self._jobs.values() if j.protected]
        for job in busy:
            log.info("waiting for the %s job %s to finish before stopping", job.kind, job.id)
        tasks = [j.task for j in self._jobs.values() if j.task and not j.task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task

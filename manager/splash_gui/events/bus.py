"""The in-process event bus behind `GET /events` (SPEC §13.3, docs/api.md §4).

`publish` may be called from any thread; delivery happens on the manager's event
loop. Each subscriber has a bounded queue: a slow client loses its oldest events
rather than holding the manager up (every stream starts with a snapshot, so a
reconnect recovers).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

log = logging.getLogger(__name__)
QUEUE_SIZE = 2000


def to_jsonable(data: Any) -> Any:
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json")
    return data


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue[tuple[str, Any]]
    client: str | None = None
    names: frozenset[str] | None = None  # None = every event

    def wants(self, event: str) -> bool:
        return self.names is None or event in self.names


@dataclass
class EventBus:
    _subs: set[Subscription] = field(default_factory=set)
    _loop: asyncio.AbstractEventLoop | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # Synchronous listeners (alerts, usage) called on the publishing thread.
    listeners: list[Callable[[str, Any], None]] = field(default_factory=list)

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    def subscriber_count(self, client: str | None = None) -> int:
        with self._lock:
            if client is None:
                return len(self._subs)
            return sum(1 for s in self._subs if s.client == client)

    def publish(self, event: str, data: Any) -> None:
        payload = to_jsonable(data)
        for listener in list(self.listeners):
            try:
                listener(event, payload)
            except Exception:
                log.exception("event listener failed for %s", event)
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(event, payload)
        else:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._deliver, event, payload)

    def _deliver(self, event: str, payload: Any) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            if not sub.wants(event):
                continue
            if sub.queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    sub.queue.get_nowait()
            sub.queue.put_nowait((event, payload))

    @contextlib.asynccontextmanager
    async def subscribe(
        self, client: str | None = None, names: frozenset[str] | None = None
    ) -> AsyncIterator[Subscription]:
        sub = Subscription(asyncio.Queue(QUEUE_SIZE), client, names)
        with self._lock:
            self._subs.add(sub)
        try:
            yield sub
        finally:
            with self._lock:
                self._subs.discard(sub)


async def stream(
    bus: EventBus,
    snapshot: Callable[[], list[tuple[str, Any]]],
    *,
    client: str | None = None,
    names: frozenset[str] | None = None,
) -> AsyncIterator[tuple[str, Any]]:
    """Snapshot events first (taken after subscribing, so nothing falls between),
    then everything published while subscribed."""
    async with bus.subscribe(client, names) as sub:
        for item in snapshot():
            yield item[0], to_jsonable(item[1])
        while True:
            yield await sub.queue.get()

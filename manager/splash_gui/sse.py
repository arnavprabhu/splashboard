"""Server-sent events in the one shape every stream uses.

`retry: 3000` once, a per-connection increasing `id`, one-line JSON `data`, and a
`: ping` comment after 15 s without an event. No replay on `Last-Event-ID`: each
stream starts with a snapshot event instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse
from pydantic import BaseModel

PING_INTERVAL_S = 15.0
RETRY_MS = 3000
HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def encode(event_id: int, event: str, data: Any) -> str:
    if isinstance(data, BaseModel):
        payload = data.model_dump_json()
    else:
        payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"id: {event_id}\nevent: {event}\ndata: {payload}\n\n"


async def frames(
    events: AsyncIterator[tuple[str, Any]], ping_interval: float = PING_INTERVAL_S
) -> AsyncIterator[str]:
    """Encode `(event, data)` pairs, with pings while the source is quiet."""
    yield f"retry: {RETRY_MS}\n\n"
    next_event = asyncio.ensure_future(anext(events))
    event_id = 0
    try:
        while True:
            done, _ = await asyncio.wait({next_event}, timeout=ping_interval)
            if not done:
                yield ": ping\n\n"
                continue
            try:
                name, data = next_event.result()
            except StopAsyncIteration:
                return
            event_id += 1
            yield encode(event_id, name, data)
            next_event = asyncio.ensure_future(anext(events))
    finally:
        next_event.cancel()
        with contextlib.suppress(BaseException):
            await next_event
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            await aclose()


def sse_response(
    events: AsyncIterator[tuple[str, Any]], ping_interval: float = PING_INTERVAL_S
) -> StreamingResponse:
    return StreamingResponse(
        frames(events, ping_interval), media_type="text/event-stream", headers=HEADERS
    )

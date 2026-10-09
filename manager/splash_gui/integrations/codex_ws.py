"""The Responses WebSocket on the Codex router.

Codex (codex-rs `responses_websocket.rs`) opens `ws://…/v1/responses` and sends one
`{"type": "response.create", …}` text frame per request; the server answers with the
Responses stream events as text frames (the objects SSE carries in `data:`) and the
request ends at `response.completed` (or `response.incomplete`/`response.failed`). The
connection is reused: a later `response.create` may carry `previous_response_id` and
only the input items added since that response, and a v2 prewarm sends
`generate: false` with the full input and expects a completed, empty response.

Each `response.create` becomes the body of the HTTP route's own request
(`router._route`), so Splash models get the same profiles, usage capture and auto-load
and the app's own models go upstream with the caller's own credentials, as over HTTP.
The full input is rebuilt here from the connection's earlier requests and outputs, so
neither the engine nor the upstream needs a response store.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Response, WebSocket, WebSocketDisconnect
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.types import Message

from ..secrets import SecretName
from ..state import get_state
from .router import _api_key_given, _matches, _route, _secret, _unauthorized, local_only
from .router import router as router

log = logging.getLogger(__name__)

# How many earlier responses a connection remembers for `previous_response_id`. Codex
# only continues from the response it just received, so a few cover retries.
HISTORY = 4
TERMINAL = {"response.completed", "response.incomplete", "response.failed", "error"}
# Handshake headers that describe the WebSocket, not the request behind it.
WS_ONLY = {"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions"}
WS_ONLY_PREFIX = "sec-websocket-"


@router.websocket("/api/codex/t/{token}/v1/{path:path}")
async def codex_ws_with_token(websocket: WebSocket, token: str, path: str) -> None:
    if not local_only(websocket):
        await websocket.send_denial_response(Response(status_code=403))
        return
    if not _matches(token, _secret(websocket, SecretName.CODEX_ROUTER)):
        await websocket.send_denial_response(
            _unauthorized("invalid Codex router token", "invalid_router_token")
        )
        return
    await _serve(websocket, path, authorized=True)


@router.websocket("/api/codex/v1/{path:path}")
async def codex_ws(websocket: WebSocket, path: str) -> None:
    if not local_only(websocket):
        await websocket.send_denial_response(Response(status_code=403))
        return
    await _serve(websocket, path, authorized=_api_key_given(websocket))


async def _serve(websocket: WebSocket, path: str, *, authorized: bool) -> None:
    if path != "responses":
        # Only `/responses` has a WebSocket form; 426 is what makes Codex use HTTP.
        await websocket.send_denial_response(Response(status_code=426))
        return
    state = get_state(websocket)
    if not state.integrations or "codex-app" not in state.integrations.records:
        await websocket.send_denial_response(Response(status_code=503))
        return
    await websocket.accept()
    log.info("codex router: Responses WebSocket opened")
    try:
        await _Connection(websocket, authorized).run()
    finally:
        log.info("codex router: Responses WebSocket closed")


def _items(value: Any) -> list[Any]:
    """`input` as a list of items (a bare string is one user message)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": value}]}
        ]
    if isinstance(value, list):
        return list(value)
    return [value]


def _error_frame(
    status: int, code: str, message: str, kind: str = "invalid_request_error"
) -> dict[str, Any]:
    """codex-rs `WrappedWebsocketErrorEvent`: read as an HTTP error with this status."""
    return {
        "type": "error",
        "status": status,
        "error": {"type": kind, "code": code, "message": message},
    }


class _Connection:
    def __init__(self, websocket: WebSocket, authorized: bool) -> None:
        self.ws = websocket
        self.authorized = authorized
        # response id -> the full input of its request plus its output items.
        self.history: OrderedDict[str, list[Any]] = OrderedDict()
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.pending: deque[str] = deque()

    async def run(self) -> None:
        reader = asyncio.create_task(self._read())
        try:
            while True:
                text = self.pending.popleft() if self.pending else await self.inbox.get()
                if text is None:
                    return
                try:
                    payload = json.loads(text)
                except ValueError:
                    await self._send(_error_frame(400, "invalid_json", "invalid JSON message"))
                    continue
                kind = payload.get("type") if isinstance(payload, dict) else None
                if kind == "response.interrupt":
                    continue  # nothing is running
                if kind != "response.create":
                    await self._send(
                        _error_frame(
                            400, "unsupported_message", f"unsupported message type {kind!r}"
                        )
                    )
                    continue
                if not await self._turn(payload):
                    return
        finally:
            reader.cancel()

    async def _read(self) -> None:
        try:
            while True:
                message: Message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                text = message.get("text")
                if text is None:
                    text = (message.get("bytes") or b"").decode("utf-8", "replace")
                await self.inbox.put(text)
        finally:
            self.inbox.put_nowait(None)

    async def _send(self, event: dict[str, Any]) -> None:
        await self.ws.send_text(json.dumps(event, separators=(",", ":"), ensure_ascii=False))

    def _remember(self, response_id: str, items: list[Any]) -> None:
        self.history[response_id] = items
        self.history.move_to_end(response_id)
        while len(self.history) > HISTORY:
            self.history.popitem(last=False)

    async def _turn(self, payload: dict[str, Any]) -> bool:
        """One `response.create`. False when the client has gone."""
        body = {
            k: v
            for k, v in payload.items()
            if k not in ("type", "previous_response_id", "generate")
        }
        previous = payload.get("previous_response_id")
        items = _items(body.get("input"))
        if previous is not None:
            baseline = self.history.get(previous) if isinstance(previous, str) else None
            if baseline is None:
                # codex-rs retries the full request on this code.
                await self._send(
                    _error_frame(
                        400,
                        "previous_response_not_found",
                        f"Previous response with id '{previous}' not found.",
                    )
                )
                return True
            items = [*baseline, *items]
        body["input"] = items
        if payload.get("generate") is False:
            await self._prewarm(body)
            log.info("codex router: WebSocket prewarm (generate=false) answered")
            return True
        body["stream"] = True
        response = await _route(self._request(body), "responses", authorized=self.authorized)
        log.info(
            "codex router: WebSocket response.create model=%s continues=%s status=%s",
            body.get("model"),
            "previous response" if previous is not None else "no",
            response.status_code,
        )
        return await self._relay(response, items)

    async def _prewarm(self, body: dict[str, Any]) -> None:
        response_id = "resp_" + uuid.uuid4().hex
        response: dict[str, Any] = {
            "id": response_id,
            "object": "response",
            "model": body.get("model"),
            "output": [],
        }
        await self._send(
            {"type": "response.created", "response": {**response, "status": "in_progress"}}
        )
        await self._send(
            {
                "type": "response.completed",
                "response": {**response, "status": "completed", "usage": None},
            }
        )
        self._remember(response_id, list(body["input"]))

    def _request(self, body: dict[str, Any]) -> Request:
        """The HTTP request this `response.create` stands for."""
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
        headers: list[tuple[bytes, bytes]] = []
        for name, value in self.ws.scope["headers"]:
            lower = name.decode("latin-1").lower()
            if lower.startswith(WS_ONLY_PREFIX) or lower in (
                "upgrade",
                "connection",
                "content-type",
                "content-length",
                "accept",
                "accept-encoding",
            ):
                continue
            if lower == "openai-beta" and b"responses_websockets" in value:
                continue
            headers.append((name.lower(), value))
        headers += [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
            (b"accept", b"text/event-stream"),
            # The bridge reads the events, so the upstream must not compress them.
            (b"accept-encoding", b"identity"),
        ]
        scope = {
            **self.ws.scope,
            "type": "http",
            "method": "POST",
            "scheme": "https" if self.ws.scope.get("scheme") == "wss" else "http",
            "http_version": "1.1",
            "headers": headers,
        }
        scope.pop("subprotocols", None)
        sent = False

        async def receive() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}

        return Request(scope, receive)

    async def _relay(self, response: Response, items: list[Any]) -> bool:
        """Send the route's answer as frames; remember a completed response."""
        try:
            if response.status_code >= 400 or "text/event-stream" not in response.headers.get(
                "content-type", ""
            ):
                raw = b"".join([chunk async for chunk in _chunks(response)])
                await self._send(_as_error(response, raw))
                return True
            return await self._stream(response, items)
        finally:
            if response.background is not None:
                await response.background()

    async def _stream(self, response: Response, items: list[Any]) -> bool:
        output: list[Any] = []
        state: dict[str, Any] = {"id": None, "done": False}

        async def pump() -> None:
            buffer = b""
            async with contextlib.aclosing(_chunks(response)) as chunks:
                async for chunk in chunks:
                    buffer += chunk.replace(b"\r\n", b"\n")
                    while b"\n\n" in buffer:
                        block, buffer = buffer.split(b"\n\n", 1)
                        event = _event(block)
                        if event is None or state["done"]:
                            # After the terminal event the rest is read, not sent, so
                            # the proxy records the request as complete.
                            continue
                        event = _codex_event(event, state["id"])
                        await self._send(event)
                        kind = event.get("type")
                        if kind == "response.created":
                            state["id"] = (event.get("response") or {}).get("id")
                        elif kind == "response.output_item.done" and event.get("item") is not None:
                            output.append(event["item"])
                        if kind in TERMINAL:
                            state["done"] = True
                            if kind == "response.completed":
                                rid = (event.get("response") or {}).get("id") or state["id"]
                                if isinstance(rid, str) and rid:
                                    self._remember(rid, [*items, *output])

        task = asyncio.create_task(pump())
        getter: asyncio.Task[str | None] | None = None
        try:
            while not task.done():
                getter = asyncio.create_task(self.inbox.get())
                done, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    continue  # the finally below cancels it; a queued message stays queued
                text, getter = getter.result(), None
                if text is None:
                    return False
                if _is_interrupt(text) and not state["done"]:
                    task.cancel()
                    await _settle(task)
                    await self._send(
                        {
                            "type": "response.incomplete",
                            "response": {
                                "id": state["id"] or "resp_" + uuid.uuid4().hex,
                                "object": "response",
                                "status": "incomplete",
                                "incomplete_details": {"reason": "interrupted"},
                                "output": [],
                                "usage": None,
                            },
                        }
                    )
                    return True
                self.pending.append(text)  # the next request, queued behind this one
        finally:
            if getter is not None:
                getter.cancel()
                await _settle(getter)
            if not task.done():
                task.cancel()
            await _settle(task)
        error = task.exception() if not task.cancelled() else None
        if isinstance(error, WebSocketDisconnect):
            return False
        if error is not None:
            log.warning("codex router: WebSocket stream failed: %s", type(error).__name__)
        if not state["done"]:
            await self._send(
                _error_frame(
                    502,
                    "stream_ended",
                    "the stream ended before response.completed",
                    "server_error",
                )
            )
        return True


def _event(block: bytes) -> dict[str, Any] | None:
    """The JSON object one SSE event carries in its `data:` lines."""
    data = "\n".join(
        line[5:].removeprefix(" ")
        for line in block.decode("utf-8", "replace").split("\n")
        if line.startswith("data:")
    )
    if not data or data == "[DONE]":
        return None
    try:
        event = json.loads(data)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _is_interrupt(text: str) -> bool:
    try:
        payload = json.loads(text)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("type") == "response.interrupt"


async def _settle(task: asyncio.Task[Any]) -> None:
    # The outcome is read from the task afterwards; here it only has to finish.
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _chunks(response: Response) -> AsyncGenerator[bytes]:
    if isinstance(response, StreamingResponse):
        iterator = response.body_iterator
        try:
            async for chunk in iterator:
                yield chunk.encode() if isinstance(chunk, str) else bytes(chunk)
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()
    else:
        yield bytes(response.body)


def _codex_event(event: dict[str, Any], response_id: str | None) -> dict[str, Any]:
    """A mid-stream engine error (`{"error": {…}}`, no status) as `response.failed`.

    codex-rs ignores an `error` event without a status, then waits out its idle timeout.
    """
    kind = event.get("type")
    if (kind is None and isinstance(event.get("error"), dict)) or (
        kind == "error" and event.get("status") is None and event.get("status_code") is None
    ):
        error: dict[str, Any] = event["error"] if isinstance(event.get("error"), dict) else {}
        return {
            "type": "response.failed",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "failed",
                "error": {
                    "code": error.get("code") or error.get("type") or "server_error",
                    "message": error.get("message") or "the engine reported an error",
                },
                "output": [],
                "usage": None,
            },
        }
    return event


def _as_error(response: Response, raw: bytes) -> dict[str, Any]:
    """An HTTP error answer as the wrapped error frame codex-rs maps to that status."""
    status = response.status_code if response.status_code >= 400 else 502
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        text = raw.decode("utf-8", "replace").strip()[:500]
        error = {
            "type": "server_error",
            "code": "upstream_error" if response.status_code >= 400 else "not_a_stream",
            "message": text or f"HTTP {response.status_code}",
        }
    frame: dict[str, Any] = {"type": "error", "status": status, "error": error}
    retry = response.headers.get("retry-after")
    if retry:
        frame["headers"] = {"retry-after": retry}
    return frame

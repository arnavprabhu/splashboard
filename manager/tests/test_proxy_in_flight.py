"""The proxy counts every forwarded request out of the engine exactly once.

An engine request left counted would keep the supervisor busy for good, which
blocks model switches and idle handling. These drive `_forward` against a mock
engine so each way a request can end is exercised directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import ClientDisconnect, Request

from splash_gui.proxy.shapes import SHAPES

PATH = "/v1/chat/completions"


def _request() -> Request:
    return Request(
        {"type": "http", "method": "POST", "path": PATH, "headers": [], "query_string": b""}
    )


@pytest.fixture
def proxy(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Any:
    state = app.state.manager
    sup = state.supervisor
    sup._run = SimpleNamespace(base_url="http://127.0.0.1:18000", key="internal")
    sup.state = "busy"
    # The state event describes a real engine run; only the counter matters here.
    monkeypatch.setattr(sup, "_publish", lambda force=False: None)
    return state.proxy


def _engine(request: httpx.Request) -> httpx.Response:
    async def body() -> AsyncIterator[bytes]:
        yield b'{"ok":true}'

    return httpx.Response(200, headers={"content-type": "application/json"}, content=body())


async def _forward(proxy: Any) -> Any:
    return await proxy._forward(
        _request(),
        "POST",
        PATH,
        {"stream": False},
        route=None,
        shape=SHAPES[PATH],
        injected={},
        cors={},
        anthropic=False,
        record=False,
    )


async def test_cancelled_before_the_engine_answers_releases_the_engine(proxy: Any) -> None:
    sup = proxy.sup
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        task = asyncio.create_task(_forward(proxy))
        await asyncio.wait_for(entered.wait(), 2)
        assert sup.in_flight == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sup.in_flight == 0
        assert not sup.busy()
    finally:
        await proxy._client.aclose()


async def _send_and_count(proxy: Any, send: Any) -> None:
    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    sup = proxy.sup
    proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(_engine))
    try:
        sup.request_started()  # another request still in flight: catches a double release
        response = await _forward(proxy)
        assert sup.in_flight == 2
        scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)
        assert sup.in_flight == 1
        # The body generator's own cleanup, if it ever runs, must not release again.
        body: AsyncGenerator[bytes] = response.body_iterator
        await body.aclose()
        assert sup.in_flight == 1
    finally:
        await proxy._client.aclose()


async def test_client_gone_before_the_first_byte_releases_the_engine_once(proxy: Any) -> None:
    async def send(message: dict[str, Any]) -> None:
        raise OSError("client disconnected")

    await _send_and_count(proxy, send)


async def test_client_gone_mid_body_releases_the_engine_once(proxy: Any) -> None:
    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            raise OSError("client disconnected")

    await _send_and_count(proxy, send)


async def test_a_complete_answer_releases_the_engine_once(proxy: Any) -> None:
    sup = proxy.sup
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(_engine))
    try:
        sup.request_started()
        response = await _forward(proxy)
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert b"".join(m.get("body", b"") for m in sent) == b'{"ok":true}'
        assert sup.in_flight == 1
    finally:
        await proxy._client.aclose()

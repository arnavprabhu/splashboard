"""Minimal MCP clients (SPEC §10.5): stdio and streamable HTTP, JSON-RPC 2.0.

Only what the chat needs: `initialize`, `notifications/initialized`, `tools/list`
(with pagination) and `tools/call`. Server-initiated requests (sampling, roots,
elicitation) are answered with JSON-RPC "method not found".
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from .. import __version__

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
INIT_TIMEOUT_S = 20.0
LIST_TIMEOUT_S = 20.0
CALL_TIMEOUT_S = 120.0


class McpError(Exception):
    pass


class Transport(Protocol):
    async def request(self, method: str, params: dict[str, Any] | None, timeout: float) -> Any: ...

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None: ...

    async def close(self) -> None: ...


def _result(message: dict[str, Any]) -> Any:
    if "error" in message and message["error"] is not None:
        error = message["error"]
        text = error.get("message") if isinstance(error, dict) else str(error)
        raise McpError(f"MCP error: {text}")
    return message.get("result")


class StdioTransport:
    """A server process speaking newline-delimited JSON-RPC on stdin/stdout."""

    def __init__(self, command: str, args: list[str], env: Mapping[str, str]) -> None:
        self.command = command
        self.args = args
        self.env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        env = {**os.environ, **self.env}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
                limit=16 * 1024 * 1024,
            )
        except OSError as error:
            raise McpError(f"cannot start {self.command}: {error}") from None
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        async for line in self._proc.stderr:
            log.debug("mcp %s: %s", self.command, line.decode("utf-8", "replace").rstrip())

    async def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            async for raw in self._proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    await self._dispatch(message)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(McpError("the MCP server exited"))
            self._pending.clear()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message and "id" in message:  # a server request we don't serve
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "method not found"},
                }
            )
            return
        ident = message.get("id")
        if isinstance(ident, int) and ident in self._pending:
            future = self._pending.pop(ident)
            if not future.done():
                future.set_result(message)

    async def _send(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.returncode is not None:
            raise McpError("the MCP server is not running")
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            try:
                self._proc.stdin.write(data)
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise McpError(f"the MCP server closed its input: {error}") from None

    async def request(self, method: str, params: dict[str, Any] | None, timeout: float) -> Any:
        ident = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": ident, "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)
        try:
            response = await asyncio.wait_for(future, timeout)
        except TimeoutError:
            self._pending.pop(ident, None)
            raise McpError(f"{method} timed out after {timeout:g} s") from None
        return _result(response)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def close(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                if proc.stdin is not None:
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 2.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 3.0)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
        for task in (self._reader, self._stderr):
            if task is not None:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task


def _sse_messages(text: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    data: list[str] = []
    for line in [*text.splitlines(), ""]:
        if not line:
            if data:
                with contextlib.suppress(ValueError):
                    value = json.loads("\n".join(data))
                    if isinstance(value, dict):
                        messages.append(value)
                    elif isinstance(value, list):
                        messages.extend(v for v in value if isinstance(v, dict))
            data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    return messages


class HttpTransport:
    """Streamable HTTP: POST each JSON-RPC message; answers come back as JSON or SSE."""

    def __init__(
        self,
        url: str,
        headers: Mapping[str, str],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(CALL_TIMEOUT_S, connect=10)
        )
        self._own_client = client is None
        self._ids = itertools.count(1)
        self.session_id: str | None = None
        self.protocol_version: str | None = None

    async def start(self) -> None:
        return None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    async def _post(self, message: dict[str, Any], timeout: float) -> httpx.Response:
        try:
            response = await self._client.post(
                self.url, json=message, headers=self._headers(), timeout=timeout
            )
        except httpx.HTTPError as error:
            raise McpError(f"cannot reach {self.url}: {error}") from None
        session = response.headers.get("mcp-session-id")
        if session:
            self.session_id = session
        if response.status_code >= 400:
            raise McpError(
                f"{self.url} answered HTTP {response.status_code}: {response.text[:200]}"
            )
        return response

    async def request(self, method: str, params: dict[str, Any] | None, timeout: float) -> Any:
        ident = next(self._ids)
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": ident, "method": method}
        if params is not None:
            message["params"] = params
        response = await self._post(message, timeout)
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            candidates = _sse_messages(response.text)
        else:
            try:
                body = response.json()
            except ValueError:
                raise McpError(f"{self.url} answered with invalid JSON") from None
            candidates = body if isinstance(body, list) else [body]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("id") == ident:
                return _result(candidate)
        raise McpError(f"{self.url} sent no answer to {method}")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._post(message, LIST_TIMEOUT_S)

    async def close(self) -> None:
        if self.session_id:
            with contextlib.suppress(Exception):
                await self._client.delete(self.url, headers=self._headers(), timeout=5)
        if self._own_client:
            await self._client.aclose()


class McpSession:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.server_info: dict[str, Any] = {}
        self.protocol_version: str | None = None

    async def initialize(self) -> None:
        result = await self.transport.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "splash-gui", "version": __version__},
            },
            INIT_TIMEOUT_S,
        )
        if not isinstance(result, dict):
            raise McpError("invalid initialize result")
        version = result.get("protocolVersion")
        self.protocol_version = version if isinstance(version, str) else PROTOCOL_VERSION
        if isinstance(self.transport, HttpTransport):
            self.transport.protocol_version = self.protocol_version
        info = result.get("serverInfo")
        self.server_info = info if isinstance(info, dict) else {}
        await self.transport.notify("notifications/initialized")

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(50):
            params = {"cursor": cursor} if cursor else {}
            result = await self.transport.request("tools/list", params, LIST_TIMEOUT_S)
            if not isinstance(result, dict):
                raise McpError("invalid tools/list result")
            tools += [t for t in result.get("tools", []) if isinstance(t, dict)]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.transport.request(
            "tools/call", {"name": name, "arguments": arguments}, CALL_TIMEOUT_S
        )
        if not isinstance(result, dict):
            raise McpError("invalid tools/call result")
        return result

    async def close(self) -> None:
        await self.transport.close()

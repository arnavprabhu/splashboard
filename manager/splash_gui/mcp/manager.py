"""MCP connections for the chat (SPEC §10.5): one pooled session per configured
server (`global.chat.mcp_servers`), reconnected when its config changes and closed
on shutdown. Tool calls need a confirmation unless the server is `always_allow`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import ApiError
from ..schemas import McpCallResult, McpServerError, McpTool, McpToolList
from ..settings.model import McpServer
from ..settings.store import Change
from .client import HttpTransport, McpError, McpSession, StdioTransport, Transport

if TYPE_CHECKING:
    from ..state import ManagerState

log = logging.getLogger(__name__)


@dataclass
class _Conn:
    config: McpServer
    session: McpSession
    tools: list[dict[str, Any]] | None = None


class McpManager:
    def __init__(self, state: ManagerState) -> None:
        self.state = state
        self._conns: dict[str, _Conn] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closing: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.state.settings_listeners.append(self._on_settings)

    async def shutdown(self) -> None:
        with contextlib.suppress(ValueError):
            self.state.settings_listeners.remove(self._on_settings)
        await asyncio.gather(*self._closing, return_exceptions=True)
        for name in list(self._conns):
            await self._close(name)

    def servers(self) -> dict[str, McpServer]:
        return dict(self.state.settings.current.global_.chat.mcp_servers)

    def _event(self, server: str, state: str, message: str | None = None) -> None:
        self.state.events.publish(
            "mcp.state", {"server": server, "state": state, "message": message}
        )

    def _on_settings(self, changes: list[Change], restart_required: bool) -> None:
        """Close sessions whose server was removed or edited (reconnect lazily)."""
        servers = self.servers()
        stale = [n for n, c in self._conns.items() if servers.get(n) != c.config]
        if not stale or self._loop is None:
            return

        def schedule() -> None:
            for name in stale:
                task = asyncio.create_task(self._close(name))
                self._closing.add(task)
                task.add_done_callback(self._closing.discard)

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            schedule()
        else:
            self._loop.call_soon_threadsafe(schedule)

    async def _close(self, name: str) -> None:
        conn = self._conns.pop(name, None)
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.session.close()
            self._event(name, "disconnected")

    def _transport(self, config: McpServer) -> Transport:
        if config.command is not None:
            return StdioTransport(config.command, list(config.args), dict(config.env))
        assert config.url is not None
        return HttpTransport(config.url, dict(config.headers))

    async def session(self, name: str) -> _Conn:
        config = self.servers().get(name)
        if config is None:
            raise ApiError(404, f"no MCP server named {name}", "mcp_server_not_found")
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            conn = self._conns.get(name)
            if conn is not None and conn.config == config:
                return conn
            if conn is not None:
                await self._close(name)
            transport = self._transport(config)
            start = getattr(transport, "start", None)
            session = McpSession(transport)
            try:
                if start is not None:
                    await start()
                await session.initialize()
            except (McpError, OSError) as error:
                with contextlib.suppress(Exception):
                    await session.close()
                self._event(name, "error", str(error))
                raise
            conn = _Conn(config=config, session=session)
            self._conns[name] = conn
            self._event(name, "connected")
            return conn

    async def _tools_of(self, name: str, config: McpServer) -> list[McpTool]:
        conn = await self.session(name)
        try:
            tools = await conn.session.list_tools()
        except McpError:
            await self._close(name)
            conn = await self.session(name)
            tools = await conn.session.list_tools()
        conn.tools = tools
        return [
            McpTool(
                server=name,
                name=str(tool.get("name", "")),
                description=tool.get("description")
                if isinstance(tool.get("description"), str)
                else None,
                input_schema=tool["inputSchema"]
                if isinstance(tool.get("inputSchema"), dict)
                else {},
                always_allow=config.always_allow,
            )
            for tool in tools
            if tool.get("name")
        ]

    async def tools(self) -> McpToolList:
        servers = {n: c for n, c in self.servers().items() if c.enabled}
        results = await asyncio.gather(
            *(self._tools_of(n, c) for n, c in servers.items()), return_exceptions=True
        )
        tools: list[McpTool] = []
        errors: list[McpServerError] = []
        for name, result in zip(servers, results, strict=True):
            if isinstance(result, BaseException):
                errors.append(
                    McpServerError(server=name, message=str(result) or type(result).__name__)
                )
            else:
                tools.extend(result)
        return McpToolList(tools=tools, errors=errors)

    async def call(
        self, server: str, tool: str, arguments: dict[str, Any], confirmed: bool
    ) -> McpCallResult:
        config = self.servers().get(server)
        if config is None:
            raise ApiError(404, f"no MCP server named {server}", "mcp_server_not_found")
        if not config.enabled:
            raise ApiError(403, f"MCP server {server} is disabled", "mcp_server_disabled")
        if not (confirmed or config.always_allow):
            raise ApiError(
                409,
                f"Run {tool} on {server}? Confirm the call, or allow this server always.",
                "needs_confirmation",
                details={"confirm_with": "confirmed", "server": server, "tool": tool},
            )
        started = time.monotonic()
        try:
            conn = await self.session(server)
            result = await conn.session.call_tool(tool, arguments)
        except McpError as error:
            await self._close(server)
            return McpCallResult(
                content=[{"type": "text", "text": str(error)}],
                is_error=True,
                duration_ms=(time.monotonic() - started) * 1000,
            )
        content = result.get("content")
        return McpCallResult(
            content=[c for c in content if isinstance(c, dict)]
            if isinstance(content, list)
            else [],
            structured_content=result.get("structuredContent"),
            is_error=bool(result.get("isError")),
            duration_ms=(time.monotonic() - started) * 1000,
        )

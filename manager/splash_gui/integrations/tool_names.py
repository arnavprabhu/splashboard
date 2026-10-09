"""Tool names a Splash model can take, and the originals back.

Splash 1.3.0 refuses a Responses request whose namespace tool has a namespace or child
name outside `[A-Za-z0-9_-]{1,64}` (400 "invalid namespace tool name",
`server/api_shapes.py` `_namespace_alias`, lines 468-475), and a plain function tool
whose name is outside `[A-Za-z0-9_-]{1,128}` (`server/tool_schema.py` `normalize_tools`,
lines 857-859). Codex allows 128 characters for an MCP namespace plus tool name
(codex-rs `codex-mcp/src/tools.rs`, `MAX_TOOL_NAME_LENGTH`), so the ChatGPT app's own
connectors send names Splash refuses.

For Splash models the router renames each such name to a valid alias before the request
reaches the engine, and names every function call in the answer back before Codex sees
it. An alias is a function of the name alone (sanitized, cut, plus a hash of the
original), so the map is rebuilt from each request and nothing is kept between turns.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any

from starlette.responses import Response, StreamingResponse

log = logging.getLogger(__name__)

NAMESPACE_LIMIT = 64  # a namespace and each of its tools (api_shapes.py `_namespace_alias`)
FUNCTION_LIMIT = 128  # a plain function tool (tool_schema.py `normalize_tools`)
HASH_LENGTH = 10
SEARCH_NAME = "tool_search"  # tool_search.NAME (codex-rs `TOOL_SEARCH_TOOL_NAME`)
_INVALID = re.compile(r"[^A-Za-z0-9_-]")


def alias(name: str, limit: int) -> str:
    """`name` when Splash takes it, else a valid name unique to it, at most `limit` long."""
    if 0 < len(name) <= limit and _INVALID.search(name) is None:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:HASH_LENGTH]
    return f"{_INVALID.sub('_', name)[: limit - HASH_LENGTH - 1]}_{digest}"


@dataclass
class ToolNames:
    """The aliases one request used, alias -> original."""

    namespaces: dict[str, str] = field(default_factory=dict)
    children: dict[str, str] = field(default_factory=dict)
    functions: dict[str, str] = field(default_factory=dict)
    # The request carried Codex's tool search (tool_search.py): a `tool_search`
    # function call in the answer goes back to Codex as a `tool_search_call`.
    search: bool = False

    def __bool__(self) -> bool:
        return bool(self.namespaces or self.children or self.functions or self.search)

    def _alias(self, table: dict[str, str], name: Any, limit: int) -> Any:
        if not isinstance(name, str):
            return name  # Splash refuses it with its own message
        new = alias(name, limit)
        if new != name:
            table[new] = name
        return new

    def namespace(self, name: Any) -> Any:
        return self._alias(self.namespaces, name, NAMESPACE_LIMIT)

    def child(self, name: Any) -> Any:
        return self._alias(self.children, name, NAMESPACE_LIMIT)

    def function(self, name: Any) -> Any:
        return self._alias(self.functions, name, FUNCTION_LIMIT)

    def call(self, item: dict[str, Any]) -> dict[str, Any]:
        """A function call (or tool_choice) with Splash's names."""
        if item.get("namespace") is not None:
            return {
                **item,
                "namespace": self.namespace(item["namespace"]),
                "name": self.child(item.get("name")),
            }
        if "name" in item:
            return {**item, "name": self.function(item["name"])}
        return item

    def original(self, item: dict[str, Any]) -> dict[str, Any]:
        """A function call from Splash with the names Codex sent."""
        name = item.get("name")
        if item.get("namespace") is not None:
            namespace = item["namespace"]
            return {
                **item,
                "namespace": self.namespaces.get(namespace, namespace),
                "name": self.children.get(name, name) if isinstance(name, str) else name,
            }
        if isinstance(name, str) and name in self.functions:
            return {**item, "name": self.functions[name]}
        return item

    def bare(self, name: Any) -> Any:
        """A name without its namespace (`response.function_call_arguments.done`)."""
        if not isinstance(name, str):
            return name
        return self.children.get(name) or self.functions.get(name) or name

    @property
    def aliases(self) -> list[str]:
        return [*self.namespaces, *self.children, *self.functions]


def alias_request(
    body: dict[str, Any], *, search: bool = False
) -> tuple[dict[str, Any], ToolNames]:
    """`body` with every tool name Splash would refuse renamed, in its tools, its named
    tool_choice and the function calls of its input. `search`: the request carried
    Codex's tool search, so the answer's `tool_search` calls are converted back."""
    names = ToolNames(search=search)
    tools = body.get("tools")
    if isinstance(tools, list):
        renamed: list[Any] = []
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") == "namespace":
                children = tool.get("tools")
                tool = {
                    **tool,
                    "name": names.namespace(tool.get("name")),
                    "tools": [
                        {**child, "name": names.child(child.get("name"))}
                        if isinstance(child, dict) and "name" in child
                        else child
                        for child in children
                    ]
                    if isinstance(children, list)
                    else children,
                }
            elif isinstance(tool, dict) and tool.get("type") == "function" and "name" in tool:
                tool = {**tool, "name": names.function(tool["name"])}
            renamed.append(tool)
        body = {**body, "tools": renamed}
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") == "function":
        body = {**body, "tool_choice": names.call(choice)}
    items = body.get("input")
    if isinstance(items, list):
        body = {
            **body,
            "input": [
                names.call(item)
                if isinstance(item, dict) and item.get("type") == "function_call"
                else item
                for item in items
            ],
        }
    if names.aliases:
        log.info(
            "codex router: renamed %d tool names Splash refuses: %s",
            len(names.aliases),
            ", ".join(f"{v} -> {k}" for k, v in _pairs(names)),
        )
    return body, names


def _pairs(names: ToolNames) -> Iterable[tuple[str, str]]:
    yield from names.namespaces.items()
    yield from names.children.items()
    yield from names.functions.items()


def restore(value: Any, names: ToolNames) -> Any:
    """A Responses event or response object with the original names in its calls."""
    if isinstance(value, list):
        return [restore(item, names) for item in value]
    if not isinstance(value, dict):
        return value
    out = {key: restore(item, names) for key, item in value.items()}
    if out.get("type") == "function_call":
        out = names.original(out)
        if names.search and out.get("namespace") is None and out.get("name") == SEARCH_NAME:
            from .tool_search import to_codex_call  # tool_search imports this module

            out = to_codex_call(out)
    elif out.get("type") == "response.function_call_arguments.done" and "name" in out:
        out["name"] = names.bare(out["name"])
    return out


def restore_response(response: Response, names: ToolNames) -> None:
    """Rewrite the body the proxy streams back, SSE or JSON, in place."""
    if not names or not isinstance(response, StreamingResponse):
        return
    if "content-length" in response.headers:
        del response.headers["content-length"]
    markers = [a.encode() for a in names.aliases]
    if names.search:
        markers.append(SEARCH_NAME.encode())
    sse = "text/event-stream" in response.headers.get("content-type", "")
    source = response.body_iterator
    response.body_iterator = (
        _restore_sse(source, names, markers) if sse else _restore_json(source, names)
    )


async def _bytes(source: AsyncIterable[Any]) -> AsyncIterator[bytes]:
    async for chunk in source:
        yield chunk.encode() if isinstance(chunk, str) else bytes(chunk)


async def _restore_sse(
    source: AsyncIterable[Any], names: ToolNames, markers: list[bytes]
) -> AsyncIterator[bytes]:
    buffer = b""
    async for chunk in _bytes(source):
        buffer += chunk
        while True:
            end = _event_end(buffer)
            if end is None:
                break
            block, buffer = buffer[:end], buffer[end:]
            yield _restore_block(block, names, markers)
    if buffer:
        yield _restore_block(buffer, names, markers)


def _event_end(buffer: bytes) -> int | None:
    """The end of the first complete SSE event (after its blank line), if any."""
    ends = [i + len(sep) for sep in (b"\n\n", b"\r\n\r\n") if (i := buffer.find(sep)) >= 0]
    return min(ends) if ends else None


def _restore_block(block: bytes, names: ToolNames, markers: list[bytes]) -> bytes:
    if not any(marker in block for marker in markers):
        return block  # most events (text deltas) never name a tool
    lines = block.split(b"\n")
    for index, line in enumerate(lines):
        if not line.startswith(b"data:"):
            continue
        text = line[5:].strip()
        try:
            event = json.loads(text)
        except ValueError:
            continue
        data = json.dumps(restore(event, names), separators=(",", ":"), ensure_ascii=False)
        cr = b"\r" if line.endswith(b"\r") else b""
        lines[index] = b"data: " + data.encode() + cr
    return b"\n".join(lines)


async def _restore_json(source: AsyncIterable[Any], names: ToolNames) -> AsyncIterator[bytes]:
    raw = b"".join([chunk async for chunk in _bytes(source)])
    try:
        body = json.loads(raw)
    except ValueError:
        yield raw
        return
    yield json.dumps(restore(body, names), separators=(",", ":"), ensure_ascii=False).encode()

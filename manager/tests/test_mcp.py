# ruff: noqa: E501  (the fixture server is one embedded script)
"""MCP servers for chat tools (SPEC §10.5): stdio and streamable HTTP clients,
tool listing, confirmation and errors (`mcp/client.py`, `mcp/manager.py`)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from splash_gui.mcp.client import HttpTransport, McpSession

SERVER = r"""
import json, sys
TOOLS = [
    {"name": "add", "description": "Add two numbers",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
    {"name": "fail", "inputSchema": {"type": "object"}},
]
for raw in sys.stdin:
    msg = json.loads(raw)
    method, ident = msg.get("method"), msg.get("id")
    if ident is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fixture", "version": "1"}}
    elif method == "tools/list":
        cursor = (msg.get("params") or {}).get("cursor")
        result = {"tools": TOOLS[1:]} if cursor else {"tools": TOOLS[:1], "nextCursor": "p2"}
    elif method == "tools/call":
        args = msg["params"]["arguments"]
        if msg["params"]["name"] == "fail":
            result = {"content": [{"type": "text", "text": "it broke"}], "isError": True}
        else:
            total = args["a"] + args["b"]
            result = {"content": [{"type": "text", "text": str(total)}], "structuredContent": {"sum": total}}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "nope"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)
"""


def configure(client: TestClient, servers: dict[str, Any]) -> None:
    response = client.put("/api/admin/mcp/servers", json={"servers": servers})
    assert response.status_code == 200, response.text


def stdio_server(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    script = tmp_path / "mcp_fixture.py"
    script.write_text(SERVER)
    return {"command": sys.executable, "args": [str(script)], **extra}


def test_stdio_tools_are_listed_across_pages(client: TestClient, tmp_path: Path) -> None:
    configure(client, {"calc": stdio_server(tmp_path)})
    listing = client.post("/api/admin/mcp/tools").json()
    assert listing["errors"] == []
    assert [t["name"] for t in listing["tools"]] == ["add", "fail"]
    add = listing["tools"][0]
    assert add["server"] == "calc" and add["description"] == "Add two numbers"
    assert add["input_schema"]["properties"]["a"] == {"type": "number"}


def test_calls_need_confirmation_unless_always_allowed(client: TestClient, tmp_path: Path) -> None:
    configure(client, {"calc": stdio_server(tmp_path)})
    body = {"server": "calc", "tool": "add", "arguments": {"a": 2, "b": 3}}
    asked = client.post("/api/admin/mcp/call", json=body)
    assert asked.status_code == 409 and asked.json()["error"]["code"] == "needs_confirmation"
    done = client.post("/api/admin/mcp/call", json={**body, "confirmed": True}).json()
    assert done["is_error"] is False and done["content"] == [{"type": "text", "text": "5"}]
    assert done["structured_content"] == {"sum": 5} and done["duration_ms"] >= 0
    configure(client, {"calc": stdio_server(tmp_path, always_allow=True)})
    assert client.post("/api/admin/mcp/call", json=body).status_code == 200


def test_tool_errors_and_unknown_servers(client: TestClient, tmp_path: Path) -> None:
    configure(client, {"calc": stdio_server(tmp_path, always_allow=True)})
    failed = client.post(
        "/api/admin/mcp/call", json={"server": "calc", "tool": "fail", "arguments": {}}
    ).json()
    assert failed["is_error"] is True and failed["content"][0]["text"] == "it broke"
    missing = client.post("/api/admin/mcp/call", json={"server": "x", "tool": "t", "arguments": {}})
    assert missing.status_code == 404
    configure(client, {"calc": stdio_server(tmp_path, enabled=False)})
    disabled = client.post(
        "/api/admin/mcp/call",
        json={"server": "calc", "tool": "add", "arguments": {}, "confirmed": True},
    )
    assert disabled.status_code == 403
    assert client.post("/api/admin/mcp/tools").json()["tools"] == [], "disabled servers are skipped"


def test_a_broken_server_is_reported_not_raised(client: TestClient, tmp_path: Path) -> None:
    configure(
        client,
        {
            "calc": stdio_server(tmp_path),
            "broken": {"command": str(tmp_path / "does-not-exist")},
        },
    )
    listing = client.post("/api/admin/mcp/tools").json()
    assert [t["name"] for t in listing["tools"]] == ["add", "fail"]
    assert [e["server"] for e in listing["errors"]] == ["broken"]


def test_invalid_server_configs_are_refused(client: TestClient) -> None:
    both = client.put(
        "/api/admin/mcp/servers", json={"servers": {"x": {"command": "a", "url": "http://b"}}}
    )
    assert both.status_code == 422
    name = client.put("/api/admin/mcp/servers", json={"servers": {"bad name": {"command": "a"}}})
    assert name.status_code == 422


def test_streamable_http_with_sse_answers_and_a_session() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        message = json.loads(request.content)
        if "id" not in message:
            return httpx.Response(202)
        method = message["method"]
        if method == "initialize":
            result: Any = {"protocolVersion": "2025-06-18", "capabilities": {}}
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": message["id"], "result": result},
                headers={"Mcp-Session-Id": "s-1"},
            )
        if method == "tools/list":
            result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
        else:
            result = {"content": [{"type": "text", "text": message["params"]["arguments"]["x"]}]}
        body = (
            'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result})}\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async def main() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = HttpTransport("http://mcp.test/mcp", {"Authorization": "Bearer t"}, client)
        session = McpSession(transport)
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("echo", {"x": "hi"})
        await session.close()
        await client.aclose()
        return tools, result

    tools, result = asyncio.run(main())
    assert tools[0]["name"] == "echo" and result["content"][0]["text"] == "hi"
    after_init = [r for r in seen[1:] if r.method == "POST"]
    assert all(r.headers["mcp-session-id"] == "s-1" for r in after_init)
    assert all(r.headers["authorization"] == "Bearer t" for r in seen)
    assert seen[-1].method == "DELETE", "the session is ended on close"

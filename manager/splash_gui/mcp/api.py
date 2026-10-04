"""MCP servers and tools (SPEC §14 MCP, §10.5). Server configs live in settings
(`global.chat.mcp_servers`)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ..errors import error_responses
from ..schemas import McpCallRequest, McpCallResult, McpServers, McpToolList
from ..settings.api import save_settings
from ..state import ManagerState, get_state
from .manager import McpManager

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


@router.get("/mcp/servers", response_model=McpServers)
def get_servers(state: State) -> McpServers:
    return McpServers(servers=state.settings.current.global_.chat.mcp_servers)


@router.put("/mcp/servers", response_model=McpServers, responses=error_responses(409, 422))
def put_servers(state: State, body: McpServers) -> McpServers:
    raw = state.settings.current.to_json_dict()
    raw["global"]["chat"]["mcp_servers"] = {
        name: server.model_dump(mode="json") for name, server in body.servers.items()
    }
    result = save_settings(state, raw)
    return McpServers(servers=result.settings.global_.chat.mcp_servers)


def _manager(state: ManagerState) -> McpManager:
    if state.mcp is None:
        state.mcp = McpManager(state)
    assert isinstance(state.mcp, McpManager)
    return state.mcp


@router.get("/mcp/tools", response_model=McpToolList)
async def tools(state: State) -> McpToolList:
    """Tools of every enabled server; a server that fails is listed under `errors`."""
    return await _manager(state).tools()


@router.post(
    "/mcp/call",
    response_model=McpCallResult,
    responses=error_responses(403, 404, 409),
)
async def call(state: State, body: McpCallRequest) -> McpCallResult:
    return await _manager(state).call(body.server, body.tool, body.arguments, body.confirmed)

"""Codex's on-demand tool search for Splash models (SPEC §11.3.2, D64, Q37).

With `supports_search_tool` on a catalog entry, Codex (openai/codex `rust-v0.162.0-alpha.2`)
keeps MCP and ChatGPT Apps connector tools out of `tools` and sends one tool instead:
`{"type": "tool_search", "execution": "client", "description", "parameters"}`
(`codex-rs/core/src/tools/handlers/tool_search_spec.rs` L93-105). The search is
**client-executed**: the model answers a `tool_search_call` item
(`{"call_id", "execution": "client", "arguments": {"query", "limit"?}}`), Codex runs BM25
over its deferred tools itself and sends the next request with a `tool_search_output`
item (`{"call_id", "status", "execution", "tools": [<namespace or function specs>]}`) in
`input`. It never adds the found tools to `tools`: the model's server is expected to
read them from that history item (`core/tests/suite/search_tool.rs` L554-830).

Splash 1.3.0 takes function and namespace tools and message, reasoning, function_call and
function_call_output items only (`server/api_shapes.py` L449-454, L494-517). So, for a
Splash model, the router:

- turns the `tool_search` tool into a plain function tool of the same name;
- turns each `tool_search_call` input item into a `function_call` and each
  `tool_search_output` into a `function_call_output` naming what was found, and adds
  the found tools to `tools` (namespaces merged by name), so they can be called;
- turns a `function_call` named `tool_search` in the answer back into a
  `tool_search_call` (`tool_names.restore`).

The search itself stays in Codex. Everything here is a function of the request, so
nothing is kept between turns.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from .tool_names import FUNCTION_LIMIT, NAMESPACE_LIMIT, alias

log = logging.getLogger(__name__)

NAME = "tool_search"  # codex-rs `TOOL_SEARCH_TOOL_NAME`
EXECUTION = "client"
SUMMARY_LIMIT = 160  # characters of each found tool's description in the output text


def has_search(body: dict[str, Any]) -> bool:
    tools = body.get("tools")
    items = body.get("input")
    return (isinstance(tools, list) and any(_is_search_tool(t) for t in tools)) or (
        isinstance(items, list)
        and any(
            isinstance(i, dict) and i.get("type") in ("tool_search_call", "tool_search_output")
            for i in items
        )
    )


def _is_search_tool(tool: Any) -> bool:
    return isinstance(tool, dict) and tool.get("type") == NAME


def to_splash(body: dict[str, Any]) -> dict[str, Any]:
    """`body` with Codex's tool search in shapes Splash takes."""
    tools = body.get("tools")
    tools = list(tools) if isinstance(tools, list) else []
    converted: list[Any] = []
    for tool in tools:
        if _is_search_tool(tool):
            converted.append(
                {
                    "type": "function",
                    "name": NAME,
                    "description": tool.get("description") or "Search for deferred tools.",
                    "parameters": tool.get("parameters")
                    or {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }
            )
        else:
            converted.append(tool)
    items = body.get("input")
    found: list[Any] = []
    new_items: list[Any] = []
    if isinstance(items, list):
        for item in items:
            kind = item.get("type") if isinstance(item, dict) else None
            if kind == "tool_search_call":
                call = _call(item)
                if call is not None:
                    new_items.append(call)
            elif kind == "tool_search_output":
                loaded = [t for t in item.get("tools") or [] if isinstance(t, dict)]
                found.extend(loaded)
                if isinstance(item.get("call_id"), str) and item["call_id"]:
                    new_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": item["call_id"],
                            "output": _summary(loaded),
                        }
                    )
            else:
                new_items.append(item)
        body = {**body, "input": new_items}
    added = _merge(converted, found)
    if found:
        log.info(
            "codex router: tool search loaded %d tool definitions into the request",
            added,
        )
    return {**body, "tools": converted} if isinstance(body.get("tools"), list) or found else body


def _call(item: dict[str, Any]) -> dict[str, Any] | None:
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None  # a server-side search; Splash has nothing to pair it with
    arguments = item.get("arguments")
    text = (
        arguments
        if isinstance(arguments, str)
        else json.dumps(arguments or {}, separators=(",", ":"), ensure_ascii=False)
    )
    return {"type": "function_call", "call_id": call_id, "name": NAME, "arguments": text}


def _plain(tool: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in tool.items() if k != "defer_loading"}


def _merge(tools: list[Any], found: list[dict[str, Any]]) -> int:
    """Add the found tools to `tools` in place; the number of functions added."""
    namespaces: dict[str, dict[str, Any]] = {}
    plain: set[str] = set()
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "namespace" and isinstance(tool.get("name"), str):
            children = tool.get("tools")
            copy = {**tool, "tools": list(children) if isinstance(children, list) else []}
            tools[index] = copy
            namespaces[tool["name"]] = copy
        elif tool.get("type") == "function" and isinstance(tool.get("name"), str):
            plain.add(tool["name"])
    added = 0
    for tool in found:
        if tool.get("type") == "namespace" and isinstance(tool.get("name"), str):
            target = namespaces.get(tool["name"])
            if target is None:
                target = {**_plain(tool), "tools": []}
                tools.append(target)
                namespaces[tool["name"]] = target
            have = {c.get("name") for c in target["tools"] if isinstance(c, dict)}
            for child in tool.get("tools") or []:
                # Splash runs function children only (api_shapes.py L504-505).
                if (
                    isinstance(child, dict)
                    and child.get("type") == "function"
                    and child.get("name") not in have
                ):
                    target["tools"].append(_plain(child))
                    have.add(child.get("name"))
                    added += 1
            if not target["tools"]:
                tools.remove(target)
                del namespaces[tool["name"]]
        elif tool.get("type") == "function" and isinstance(tool.get("name"), str):
            if tool["name"] not in plain:
                tools.append(_plain(tool))
                plain.add(tool["name"])
                added += 1
    return added


def splash_name(namespace: str | None, name: str) -> str:
    """The name the model sees for a tool: Splash's `_namespace_alias` join
    (`server/api_shapes.py` L468-479) of the router's aliases."""
    if namespace is None:
        return alias(name, FUNCTION_LIMIT)
    namespace, name = alias(namespace, NAMESPACE_LIMIT), alias(name, NAMESPACE_LIMIT)
    joined = f"{namespace}__{name}"
    if len(joined) <= 64:
        return joined
    digest = hashlib.sha256(f"{namespace}\0{name}".encode()).hexdigest()[:16]
    return f"{namespace[:20]}__{name[:24]}__{digest}"


def _summary(found: list[dict[str, Any]]) -> str:
    """The `function_call_output` text for a search: what can now be called."""
    lines: list[str] = []
    for tool in found:
        if tool.get("type") == "namespace" and isinstance(tool.get("name"), str):
            for child in tool.get("tools") or []:
                if isinstance(child, dict) and child.get("type") == "function":
                    name = child.get("name")
                    if isinstance(name, str):
                        lines.append(_line(splash_name(tool["name"], name), child))
        elif tool.get("type") == "function" and isinstance(tool.get("name"), str):
            lines.append(_line(splash_name(None, tool["name"]), tool))
    if not lines:
        return "No tools matched. Try another query."
    return "These tools are now available; call them directly:\n" + "\n".join(lines)


def _line(name: str, tool: dict[str, Any]) -> str:
    description = " ".join(str(tool.get("description") or "").split())
    if len(description) > SUMMARY_LIMIT:
        description = description[: SUMMARY_LIMIT - 1] + "…"
    return f"- {name}: {description}" if description else f"- {name}"


def to_codex_call(item: dict[str, Any]) -> dict[str, Any]:
    """A `function_call` to `tool_search` from Splash as Codex's `tool_search_call`
    (`protocol/src/models.rs` L1094-1108; arguments are an object, L4108-4150)."""
    raw = item.get("arguments")
    try:
        arguments = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        arguments = None
    if not isinstance(arguments, dict):
        arguments = {"query": raw if isinstance(raw, str) else ""}
    params: dict[str, Any] = {"query": str(arguments.get("query") or "")}
    limit = arguments.get("limit")
    # codex-rs `SearchToolCallParams.limit` is a usize; 3.0 or "3" would fail to parse.
    try:
        if limit is not None and float(limit) >= 1 and float(limit) == int(float(limit)):
            params["limit"] = int(float(limit))
    except (TypeError, ValueError):
        pass
    out: dict[str, Any] = {"type": "tool_search_call"}
    if item.get("id") is not None:
        out["id"] = item["id"]
    out["call_id"] = item.get("call_id")
    if item.get("status") is not None:
        out["status"] = item["status"]
    out["execution"] = EXECUTION
    out["arguments"] = params
    return out

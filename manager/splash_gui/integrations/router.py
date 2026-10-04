"""Loopback-only Codex routing; upstream credentials never reach Splash."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
from fastapi import APIRouter, Request, Response
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from ..auth.guard import is_loopback_client
from ..settings.parsers import parse_authority
from ..state import get_state

router = APIRouter(include_in_schema=False)
HOP = {"host", "connection", "keep-alive", "transfer-encoding", "upgrade", "content-length"}


def local_only(request: Request) -> bool:
    try:
        host, _ = parse_authority(request.headers.get("host", ""))
    except ValueError:
        return False
    return (
        is_loopback_client(request.scope.get("client"))
        and not request.headers.get("origin")
        and host in ("127.0.0.1", "localhost", "::1")
        and len(request.headers.getlist("host")) == 1
    )


@router.api_route("/api/codex/v1/{path:path}", methods=["GET", "POST", "DELETE"])
async def codex(request: Request, path: str) -> Response:
    if not local_only(request):
        return Response(status_code=403)
    if request.headers.get("upgrade", "").lower() == "websocket":
        return Response(status_code=426)
    state = get_state(request)
    if not state.integrations or "codex-app" not in state.integrations.records:
        return Response(status_code=503)
    raw = await request.body()
    body: Any = {}
    if raw:
        try:
            body = json.loads(raw)
        except ValueError:
            return Response(status_code=400)
        if not isinstance(body, dict):
            return Response(status_code=400)
    allow = json.loads((state.paths.codex_app_dir / "routing.json").read_text())
    if body.get("model") in allow and request.method == "POST":
        if path not in ("responses", "chat/completions", "completions"):
            return Response(status_code=404)
        return cast(
            Response,
            await state.proxy.handle(
                request, "/v1/" + path, body_override=body, checked=True, client_label="codex-app"
            ),
        )
    upstream = (
        "https://chatgpt.com/backend-api/codex/"
        if request.headers.get("chatgpt-account-id")
        else "https://api.openai.com/v1/"
    )
    # Prefix is fixed; path may not escape to an arbitrary upstream or endpoint tree.
    if ".." in path.split("/") or path.startswith("/") or "://" in path:
        return Response(status_code=400)
    client = httpx.AsyncClient(timeout=600)
    try:
        upstream_request = client.build_request(
            request.method,
            upstream + path,
            params=request.query_params,
            content=raw,
            headers={k: v for k, v in request.headers.items() if k.lower() not in HOP},
        )
        response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError:
        await client.aclose()
        return Response(status_code=502)

    async def close() -> None:
        await response.aclose()
        await client.aclose()

    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        headers={k: v for k, v in response.headers.items() if k.lower() not in HOP},
        background=BackgroundTask(close),
    )

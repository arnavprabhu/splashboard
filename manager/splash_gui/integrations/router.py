"""Loopback-only Codex routing; upstream credentials never reach Splash.

D58: requests for a Splash model need a credential. Connect writes
`/api/codex/t/<router token>/v1` as the Codex app's `openai_base_url`; the token is in
the Keychain (`ai.splashgui.codexrouter`) and Disconnect/Restore delete it. The plain
`/api/codex/v1` path (configs written before D58) needs the API key for Splash models.
Requests for the app's own models go upstream with the caller's own credentials.
"""

from __future__ import annotations

import hmac
import json
from typing import Any, cast

import httpx
from fastapi import APIRouter, Request, Response
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from ..auth.guard import is_loopback_client
from ..secrets import SecretName, SecretsError
from ..settings.parsers import parse_authority
from ..state import get_state

router = APIRouter(include_in_schema=False)
# Hop-by-hop headers, plus `cookie`: the manager's own cookies never go to OpenAI.
HOP = {
    "host",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "cookie",
}


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


def _unauthorized(message: str, code: str) -> Response:
    return Response(
        json.dumps({"error": {"message": message, "type": "authentication_error", "code": code}}),
        status_code=401,
        media_type="application/json",
    )


def _matches(value: str | None, secret: str | None) -> bool:
    return bool(value and secret) and hmac.compare_digest(str(value).encode(), str(secret).encode())


def _secret(request: Request, name: SecretName) -> str | None:
    try:
        return get_state(request).secrets.get(name)
    except SecretsError:
        return None


def _api_key_given(request: Request) -> bool:
    key = _secret(request, SecretName.API_KEY)
    authorization = request.headers.getlist("authorization")
    supplied = list(request.headers.getlist("x-api-key"))
    for value in authorization:
        scheme, _, token = value.partition(" ")
        if scheme.lower() == "bearer":
            supplied.append(token.strip())
    return any(_matches(value, key) for value in supplied)


@router.api_route("/api/codex/t/{token}/v1/{path:path}", methods=["GET", "POST", "DELETE"])
async def codex_with_token(request: Request, token: str, path: str) -> Response:
    if not local_only(request):
        return Response(status_code=403)
    if not _matches(token, _secret(request, SecretName.CODEX_ROUTER)):
        return _unauthorized("invalid Codex router token", "invalid_router_token")
    return await _route(request, path, authorized=True)


@router.api_route("/api/codex/v1/{path:path}", methods=["GET", "POST", "DELETE"])
async def codex(request: Request, path: str) -> Response:
    if not local_only(request):
        return Response(status_code=403)
    return await _route(request, path, authorized=_api_key_given(request))


async def _route(request: Request, path: str, *, authorized: bool) -> Response:
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
        if not authorized:
            return _unauthorized("invalid or missing API key", "authentication_error")
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

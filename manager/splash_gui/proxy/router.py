"""Public API routes on the manager's port (SPEC §7.1).

| Route | Handling |
|---|---|
| POST generation routes (GENERATION_ROUTES) | routing, injection, usage capture |
| GET/DELETE /v1/responses/{id} | passed through |
| GET /v1/models, /v1/models/{id} | built by the manager (§7.3) |
| GET /ready | the engine's /ready (503 while stopped/starting) |
| GET /status, /metrics | the engine's; /metrics adds splash_gui_* gauges |

`/health`, `/` and `/admin` belong to app.py; the admin guard covers `/api/admin`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from ..state import get_state
from .pipeline import ProxyError, ProxyPipeline

router = APIRouter(include_in_schema=False)

GENERATION_ROUTES = (
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/responses",
    "/v1/messages",
    "/v1/messages/count_tokens",
    "/v1/judgments",
    "/v1/systemone",
    "/tokenize",
    "/apply-template",
)


def pipeline(request: Request) -> ProxyPipeline:
    proxy = get_state(request).proxy
    assert isinstance(proxy, ProxyPipeline)
    return proxy


def _generation(path: str) -> None:
    async def post(request: Request) -> Response:
        return await pipeline(request).handle(request, path)

    async def options(request: Request) -> Response:
        return pipeline(request).preflight(request, "POST, OPTIONS")

    router.add_api_route(path, post, methods=["POST"])
    router.add_api_route(path, options, methods=["OPTIONS"])


for _path in GENERATION_ROUTES:
    _generation(_path)


@router.get("/v1/models")
async def list_models(request: Request) -> Response:
    return await pipeline(request).models_response(request)


@router.get("/v1/models/{model_id:path}")
async def get_model(request: Request, model_id: str) -> Response:
    return await pipeline(request).models_response(request, model_id)


@router.get("/v1/responses/{response_id}")
async def get_response(request: Request, response_id: str) -> Response:
    return await pipeline(request).passthrough(request, "GET", f"/v1/responses/{response_id}")


@router.delete("/v1/responses/{response_id}")
async def delete_response(request: Request, response_id: str) -> Response:
    return await pipeline(request).passthrough(request, "DELETE", f"/v1/responses/{response_id}")


@router.get("/ready")
async def ready(request: Request) -> Response:
    return await pipeline(request).ready(request)


@router.get("/status")
async def status(request: Request) -> Response:
    return await pipeline(request).status(request)


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    return await pipeline(request).metrics(request)


@router.api_route(
    "/v1/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
)
async def unknown_v1(request: Request, rest: str) -> Response:
    proxy = pipeline(request)
    anthropic = rest.startswith("messages")
    if request.method == "OPTIONS":
        return proxy.preflight(request, "GET, POST, DELETE, OPTIONS")
    try:
        proxy.check(request)
    except ProxyError as error:
        return error.response(anthropic)
    return ProxyError(404, "not found", "not_found").response(anthropic)

"""FastAPI application factory (SPEC §4, §7.1, §14).

Route layout on the public port:
  /health                 manager liveness (always 200)
  /                       redirect to /admin/
  /admin, /admin/*        the built web SPA (hashed assets immutable; SPA fallback)
  /api/admin/*            the admin API, one router per area (ADMIN_ROUTERS)
  everything else         the proxy seam (proxy/router.py)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic.json_schema import models_json_schema

from . import __version__
from .auth.api import router as auth_router
from .auth.guard import AdminGuard
from .benchmark.api import router as benchmark_router
from .chats.api import router as chats_router
from .data.api import router as data_router
from .downloads.api import router as downloads_router
from .engine.api import router as engine_router
from .errors import install_error_handlers
from .events.api import router as events_router
from .integrations.api import router as integrations_router
from .logs.api import router as logs_router
from .mcp.api import router as mcp_router
from .metrics.api import router as metrics_router
from .models.api import router as models_router
from .paths import Paths
from .proxy.router import router as proxy_router
from .schemas import SSE_MODELS
from .secrets import SecretStore, backend_from_env
from .settings.api import router as settings_router
from .settings.store import SettingsStore
from .state import ManagerState
from .storage.api import router as storage_router
from .system.api import router as system_router
from .usage.api import router as usage_router

ADMIN_PREFIX = "/api/admin"
WEB_DIST_ENV = "SPLASH_GUI_WEB_DIST"
IMMUTABLE = "public, max-age=31536000, immutable"

# Order matters: settings (which owns /models/{id}/profiles) precedes models, whose
# `{model_id:path}` catch-all would otherwise match the profiles suffix.
ADMIN_ROUTERS: tuple[tuple[str, APIRouter], ...] = (
    ("System", system_router),
    ("Engine", engine_router),
    ("Events", events_router),
    ("Metrics", metrics_router),
    ("Settings", settings_router),
    ("Models", models_router),
    ("Downloads", downloads_router),
    ("Chats", chats_router),
    ("MCP", mcp_router),
    ("Usage", usage_router),
    ("Benchmark", benchmark_router),
    ("Integrations", integrations_router),
    ("Logs", logs_router),
    ("Data", data_router),
    ("Storage", storage_router),
    ("Auth", auth_router),
)


def default_web_dist() -> Path:
    override = os.environ.get(WEB_DIST_ENV)
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "web" / "dist"


@dataclass
class AppConfig:
    paths: Paths | None = None
    web_dist: Path | None = None
    secrets: SecretStore | None = None
    settings: SettingsStore | None = None


def build_state(config: AppConfig) -> ManagerState:
    paths = config.paths or Paths.from_env()
    paths.ensure()
    settings = config.settings or SettingsStore(paths)
    settings.load()
    secrets = config.secrets or SecretStore(backend_from_env(paths))
    state = ManagerState(
        paths=paths,
        settings=settings,
        secrets=secrets,
        web_dist=config.web_dist or default_web_dist(),
    )
    state.auth.ensure_cli_token()
    return state


def _not_built(dist: Path) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><title>Splash GUI</title><p>The web admin is not built. "
        f"Run <code>pnpm build</code> in <code>web/</code> (looked in {dist}).</p>",
        status_code=503,
    )


def spa_response(dist: Path | None, path: str) -> Response:
    """Serve a file from the SPA build, or index.html for client-side routes."""
    if dist is None or not (dist / "index.html").is_file():
        return _not_built(dist or Path("web/dist"))
    root = dist.resolve()
    if path:
        candidate = (root / path).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            cache = IMMUTABLE if candidate.relative_to(root).parts[0] == "assets" else "no-cache"
            return FileResponse(candidate, headers={"Cache-Control": cache})
        if "." in Path(path).name or path.startswith("assets/"):
            return JSONResponse(
                {
                    "error": {
                        "message": f"/admin/{path} not found",
                        "type": "not_found_error",
                        "code": "not_found",
                    }
                },
                status_code=404,
            )
    return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})


def create_app(config: AppConfig | None = None) -> FastAPI:
    state = build_state(config or AppConfig())
    app = FastAPI(
        title="Splash GUI manager",
        version=__version__,
        openapi_url=f"{ADMIN_PREFIX}/openapi.json",
        docs_url=f"{ADMIN_PREFIX}/docs",
        redoc_url=None,
        separate_input_output_schemas=False,
    )
    app.state.manager = state
    install_error_handlers(app)

    @app.get("/health", tags=["Public"])
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/admin/", status_code=307)

    @app.get("/admin", include_in_schema=False)
    def admin_index() -> Response:
        return spa_response(state.web_dist, "")

    @app.get("/admin/{path:path}", include_in_schema=False)
    def admin_files(path: str) -> Response:
        return spa_response(state.web_dist, path)

    for tag, router in ADMIN_ROUTERS:
        app.include_router(router, prefix=ADMIN_PREFIX, tags=[tag])
    app.include_router(proxy_router)
    app.add_middleware(AdminGuard, settings=state.settings, auth=state.auth)
    app.openapi = lambda: custom_openapi(app)  # type: ignore[method-assign]
    return app


def custom_openapi(app: FastAPI) -> dict[str, Any]:
    """FastAPI's schema plus the SSE-only payload models (docs/api.md, Events)."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    _, defs = models_json_schema(
        [(model, "serialization") for model in SSE_MODELS],
        ref_template="#/components/schemas/{model}",
    )
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for name, definition in defs.get("$defs", {}).items():
        components.setdefault(name, definition)
    app.openapi_schema = schema
    return schema

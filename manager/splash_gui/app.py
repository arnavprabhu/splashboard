"""FastAPI application factory.

Route layout on the public port:
  /health                 manager liveness (always 200)
  /                       redirect to /admin/
  /admin, /admin/*        the built web SPA (hashed assets immutable; SPA fallback)
  /api/admin/*            the admin API, one router per area (ADMIN_ROUTERS)
  everything else         the proxy seam (proxy/router.py)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic.json_schema import models_json_schema

from . import SERVICE, __version__
from .auth.api import router as auth_router
from .auth.guard import AdminGuard
from .benchmark.api import router as benchmark_router
from .chats.api import router as chats_router
from .cli.api import router as cli_router
from .data.api import router as data_router
from .downloads.api import router as downloads_router
from .engine.api import router as engine_router
from .errors import install_error_handlers
from .events.api import router as events_router
from .hardening import Hardening, spa_csp
from .integrations.api import router as integrations_router
from .integrations.codex_ws import router as codex_router  # the router, with its WebSocket routes
from .logs.api import router as logs_router
from .mcp.api import router as mcp_router
from .metrics.api import router as metrics_router
from .models.api import router as models_router
from .packaged import bundle_root
from .paths import Paths
from .proxy.router import router as proxy_router
from .schemas import SSE_MODELS
from .secrets import SecretStore, backend_from_env
from .settings.api import router as settings_router
from .settings.store import SettingsStore
from .state import ManagerState
from .storage.api import router as storage_router
from .system.api import router as system_router
from .uninstall import router as uninstall_router
from .usage.api import router as usage_router

log = logging.getLogger(__name__)

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
    ("CLI", cli_router),
    ("Logs", logs_router),
    ("Data", data_router),
    ("Storage", storage_router),
    ("Auth", auth_router),
    ("Uninstall", uninstall_router),
)


def default_web_dist() -> Path:
    """The web admin's built files: the env override, then the bundle's own copy when
    this runs from `Splashboard.app`, then the source tree's `web/dist`."""
    override = os.environ.get(WEB_DIST_ENV)
    if override:
        return Path(override).expanduser()
    root = bundle_root()
    if root is not None:
        return root / "Contents" / "Resources" / "web"
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
    _recover_storage_move(paths, settings)
    secrets = config.secrets or SecretStore(backend_from_env(paths))
    state = ManagerState(
        paths=paths,
        settings=settings,
        secrets=secrets,
        web_dist=config.web_dist or default_web_dist(),
    )
    # Move items from the old prefix first, or ensure_api_key would create a new key
    # beside the user's old one and the migration would keep the new one.
    _migrate_keychain_prefix(state)
    state.auth.ensure_cli_token()
    state.auth.ensure_api_key()  # sign-in needs a key from the first start
    attach_core(state)
    return state


def _recover_storage_move(paths: Paths, settings: SettingsStore) -> None:
    """Settle a storage move a killed manager left halfway, before anything reads
    the models or cache folder. Its warnings show with the settings' load warnings."""
    from .storage.recovery import recover

    try:
        settings.load_warnings.extend(recover(paths, settings))
    except Exception:
        log.exception("could not settle an interrupted storage move")


def attach_core(state: ManagerState) -> None:
    """The supervisor, proxy and metrics hub exist from the start (routes use them);
    their background tasks start with the app lifespan."""
    from .engine.supervisor import Supervisor
    from .metrics.live import MetricsHub
    from .proxy.pipeline import ProxyPipeline

    supervisor = Supervisor(state)
    state.supervisor = supervisor
    state.active_model = supervisor.active_model
    state.raw_status = lambda: supervisor.status
    state.proxy = ProxyPipeline(state)
    state.metrics = MetricsHub(state)


def subsystem_services(state: ManagerState) -> list[Any]:
    """Every optional subsystem's service, in start order (shut down in reverse)."""
    from .benchmark import service as benchmark_service
    from .chats import service as chats_service
    from .downloads import service as downloads_service
    from .engine import updates
    from .integrations import service as integrations_service
    from .mcp import service as mcp_service
    from .models import service as models_service

    services = []
    for factory in (
        models_service.create,
        downloads_service.create,
        chats_service.create,
        mcp_service.create,
        benchmark_service.create,
        integrations_service.create,
        updates.create,
    ):
        service = factory(state)
        if service is not None:
            services.append(service)
    return services


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: ManagerState = app.state.manager
    state.events.attach(asyncio.get_running_loop())
    with contextlib.suppress(Exception):
        await asyncio.to_thread(state.engine)
    _read_engine_options_in_background(state)
    _ensure_shim(state)
    _migrate_mcp_secrets(state)
    core = [state.proxy, state.supervisor, state.metrics]
    services = subsystem_services(state)
    started: list[Any] = []
    try:
        for service in (*core, *services):
            await service.start()
            started.append(service)
        yield
    finally:
        for service in reversed(started):
            try:
                await service.shutdown()
            except Exception:
                log.exception("shutdown of %s failed", type(service).__name__)
        with contextlib.suppress(Exception):
            await state.jobs.shutdown()
        state.usage.close_open_sessions()


def _read_engine_options_in_background(state: ManagerState) -> None:
    """Read the engine's serve options as the manager starts, on a daemon
    thread, so the first Settings load does not wait for the helper (up to 20 s).
    A failure is logged; Settings then shows the helper's error as before."""

    def read() -> None:
        try:
            state.engine_options.get(state.engine())
        except Exception:
            log.warning("could not read the engine's serve options", exc_info=True)

    threading.Thread(target=read, name="serve-options", daemon=True).start()


def _migrate_keychain_prefix(state: ManagerState) -> None:
    """Move Keychain items from an earlier service prefix to KEYCHAIN_PREFIX, before
    anything reads them (build_state runs it before ensure_api_key). A no-op without
    LEGACY_PREFIXES."""
    from .mcp.secrets import _current_values, secret_name
    from .secrets import KEYCHAIN_PREFIX, LEGACY_PREFIXES, SecretName, migrate_prefix, suffix_of

    if not LEGACY_PREFIXES:
        return
    suffixes = [suffix_of(name) for name in SecretName]
    with contextlib.suppress(Exception):
        for server, kind, key in _current_values(state.settings.current):
            suffixes.append(suffix_of(secret_name(server, kind, key)))
    for old in LEGACY_PREFIXES:
        try:
            report = migrate_prefix(state.secrets.backend, suffixes, old, KEYCHAIN_PREFIX)
        except Exception:
            log.warning("could not move Keychain items from %s", old, exc_info=True)
            continue
        if any(report.values()):
            log.info("Keychain items from %s: %s", old, report)


def _migrate_mcp_secrets(state: ManagerState) -> None:
    """Plaintext MCP env/header values left in settings.json move to the
    Keychain at startup (settings.json keeps only references)."""
    from .mcp.secrets import migrate

    try:
        moved = migrate(state.settings, state.secrets)
    except Exception:
        log.exception("could not move MCP secrets to the Keychain")
        return
    if moved:
        log.info("moved %d MCP server secret(s) from settings.json to the Keychain", moved)


def _ensure_shim(state: ManagerState) -> None:
    """Keep `~/.splash/bin/splash` present and current.

    Writing our own script is idempotent and cheap, so it happens on every
    start; the user's rc files are never touched here — adding the PATH block is
    something the user agrees to in the app.
    """
    from .cli import install as shim

    try:
        wanted = shim.render(shim.interpreter_command())
        if state.paths.shim.is_file() and state.paths.shim.read_bytes() == wanted:
            return
        shim.install_shim(state.paths)
    except OSError:
        log.warning("could not install the splash CLI shim", exc_info=True)


def _not_built(dist: Path) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><title>Splashboard</title><p>The web admin is not built. "
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
    index = root / "index.html"
    return FileResponse(
        index, headers={"Cache-Control": "no-cache", "Content-Security-Policy": spa_csp(index)}
    )


def create_app(config: AppConfig | None = None) -> FastAPI:
    state = build_state(config or AppConfig())
    app = FastAPI(
        title="Splashboard manager",
        version=__version__,
        openapi_url=f"{ADMIN_PREFIX}/openapi.json",
        # Swagger UI loads its scripts from a CDN onto the admin origin; the schema stays.
        docs_url=None,
        redoc_url=None,
        separate_input_output_schemas=False,
        lifespan=lifespan,
    )
    app.state.manager = state
    install_error_handlers(app)

    @app.get("/health", tags=["Public"])
    def health() -> dict[str, Any]:
        return {"status": "ok", "service": SERVICE, "version": __version__}

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
    app.include_router(codex_router)
    app.add_middleware(AdminGuard, settings=state.settings, auth=state.auth)
    app.add_middleware(Hardening)
    app.openapi = lambda: custom_openapi(app)  # type: ignore[method-assign]
    return app


def custom_openapi(app: FastAPI) -> dict[str, Any]:
    """FastAPI's schema plus the SSE-only payload models."""
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

"""The public API proxy: checks, routing and auto-load, injection,
streaming forwarding and usage capture.

- **Checks**: Host allowlist, Origin (same-origin, `server.allowed_origins`
  or `'*'`; preflights answered), and the API key when `security.api_key_required`
  is on (Bearer or `x-api-key`; the CLI token from loopback and a same-origin admin
  session also pass).
- **Routing**: active model or alias → forward; `<active>:<profile>` →
  overlay; another installed model → switch when idle (busy → 503 Retry-After 10, or
  wait with `routing.switch_when_busy = wait` / `X-Splash-Switch: wait`); engine
  stopped → start it (or `routing.default_model`); else 404 `model_not_found`.
- **Forwarding**: bodies streamed back unbuffered; `Host` rewritten, credentials
  replaced with the internal key, `Origin` dropped.
- **Errors** made here use Splash's JSON shapes (Anthropic's on `/v1/messages`).
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import cookie_parser

from ..auth.core import SESSION_COOKIE
from ..auth.guard import allowed_hosts, bearer, is_local_client, is_same_origin
from ..secrets import SecretName, SecretsError
from ..settings import parsers as p
from ..settings.effective import effective_profiles, sampling_defaults
from ..usage.db import CANCELLED, CANCELLED_STATUS, iso
from .shapes import SHAPES, Shape, UsageCapture, guess_client, inject

if TYPE_CHECKING:
    from ..state import ManagerState

log = logging.getLogger(__name__)

DEFAULT_CONTEXT = 262_144
FORWARD_RESPONSE_HEADERS = (
    "retry-after",
    "www-authenticate",
    "content-type",
    "x-typesafe-request-id",
    "cache-control",
)
DROP_REQUEST_HEADERS = frozenset(
    {
        "host",
        "authorization",
        "x-api-key",
        "origin",
        "cookie",
        "content-length",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "te",
        "proxy-authorization",
        "x-splash-switch",
        "sec-fetch-site",
        "sec-fetch-mode",
        "sec-fetch-dest",
        "referer",
    }
)
REQUEST_ID_HEADER = "x-splash-request-id"
SWITCH_HEADER = "x-splash-switch"
ERROR_ALERTS = {
    "capacity_exhausted": (
        "capacity_exhausted",
        "Request refused: not enough memory for its context",
        "Lower the context limit, raise --max-memory or enable the SSD cache",
    ),
    "resource_timeout": (
        "resource_timeout",
        "Request timed out waiting for memory — close memory-heavy apps or use --language-only",
        "",
    ),
    "mask_timeout": ("mask_timeout", "Structured output mask timed out", ""),
    "engine_failed": (
        "engine_failed",
        "Engine stopped after repeated failures",
        "Restart the engine",
    ),
}


class ProxyError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        code: str,
        *,
        headers: dict[str, str] | None = None,
        details: dict[str, Any] | None = None,
        validation: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.headers = headers or {}
        self.details = details
        # A body that is not a JSON object: Splash's RequestValidationError.
        self.validation = validation

    def response(
        self,
        anthropic: bool = False,
        extra: dict[str, str] | None = None,
        *,
        systemone: bool = False,
    ) -> JSONResponse:
        headers = {**self.headers, **(extra or {})}
        if self.status == 401:
            headers.setdefault("WWW-Authenticate", "Bearer")
        if systemone:
            # server/errors.py SystemOneErrors (1.3.0): invalid fields are 422 with
            # FastAPI's `detail`, an overload is TypeSafe's 529; the rest as OpenAI's.
            if self.validation:
                detail = [{"loc": ["body"], "msg": self.message, "type": "value_error"}]
                return JSONResponse({"detail": detail}, status_code=422, headers=headers)
            if self.status == 503:
                return ProxyError(
                    529, self.message, self.code, headers=headers, details=self.details
                ).response()
        if anthropic:
            kind = {
                400: "invalid_request_error",
                401: "authentication_error",
                403: "permission_error",
                404: "not_found_error",
                409: "invalid_request_error",
                429: "rate_limit_error",
                503: "overloaded_error",
                504: "timeout_error",
            }.get(self.status, "api_error" if self.status >= 500 else "invalid_request_error")
            body: dict[str, Any] = {
                "type": "error",
                "error": {"type": kind, "message": self.message},
            }
        else:
            error: dict[str, Any] = {
                "message": self.message,
                "type": "server_error" if self.status >= 500 else "invalid_request_error",
                "code": self.code,
            }
            if self.details:
                error["details"] = self.details
            body = {"error": error}
        return JSONResponse(body, status_code=self.status, headers=headers)


def unavailable(message: str = "The engine is not ready", retry: int = 5) -> ProxyError:
    return ProxyError(503, message, "engine_unavailable", headers={"Retry-After": str(retry)})


@dataclass
class Route:
    """Where one request goes after routing."""

    model: str | None  # the model the engine serves (None: leave the body's model alone)
    request_model: str | None  # what the client asked for
    profile: str | None
    overlay: dict[str, Any]


class ProxyPipeline:
    def __init__(self, state: ManagerState) -> None:
        self.state = state
        self._client: httpx.AsyncClient | None = None
        self._refused_origins: set[str] = set()

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=5.0), limits=httpx.Limits(max_connections=200)
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
        return self._client

    @property
    def sup(self) -> Any:
        return self.state.supervisor

    # Checks ------------------------------------------------------------

    def check(self, request: Request, *, public: bool = False) -> str | None:
        """Host, Origin and API key. Returns the Access-Control-Allow-Origin value owed."""
        headers = request.headers
        g = self.state.settings.current.global_
        server = request.scope.get("server")
        local = str(server[0]) if server else None
        hosts = headers.getlist("host")
        origins = headers.getlist("origin")
        if len(hosts) != 1 or len(origins) > 1:
            raise ProxyError(
                403, "expected one Host header and at most one Origin header", "forbidden"
            )
        try:
            host, _ = p.parse_authority(hosts[0])
        except ValueError:
            raise ProxyError(403, "invalid Host header", "forbidden") from None
        if host not in allowed_hosts(g.server.host, g.server.allowed_hosts, local):
            raise ProxyError(
                403,
                f"Host {host} is not allowed; add it to Settings → Server & network → Allowed "
                "hosts (server.allowed_hosts) to accept it",
                "host_not_allowed",
                details={"host": host, "setting": "server.allowed_hosts"},
            )
        allow_origin = self._check_origin(origins[0] if origins else None, hosts[0])
        if not public and request.method != "OPTIONS" and g.security.api_key_required:
            self._authenticate(request)
        return allow_origin

    def _check_origin(self, origin: str | None, host_header: str) -> str | None:
        if origin is None:
            return None
        allowed = self.state.settings.current.global_.server.allowed_origins
        if p.ANY_ORIGIN in allowed:
            return p.ANY_ORIGIN
        if is_same_origin(origin, host_header):
            return None
        try:
            parsed = p.parse_origin(origin)
        except ValueError:
            raise ProxyError(403, "invalid Origin header", "forbidden") from None
        for entry in allowed:
            try:
                if p.parse_origin(entry) == parsed:
                    return origin
            except ValueError:
                continue
        if origin not in self._refused_origins:
            self._refused_origins.add(origin)
            log.warning(
                "refused requests from origin %s; add it to server.allowed_origins to allow it",
                origin,
            )
        raise ProxyError(
            403,
            f"Origin {origin} is not allowed; add it to Settings → Server & network → Allowed "
            "origins (server.allowed_origins) to accept it",
            "origin_not_allowed",
            details={"origin": origin, "setting": "server.allowed_origins"},
        )

    def _authenticate(self, request: Request) -> None:
        headers = request.headers
        # Splash's rule (server/http_security.py authenticate): every credential given
        # must be the key, and each header may appear only once.
        authorization = headers.getlist("authorization")
        api_keys = headers.getlist("x-api-key")
        supplied: list[str | None] = list(api_keys)
        for value in authorization:
            scheme, sep, token = value.partition(" ")
            supplied.append(token.strip() if sep and scheme.lower() == "bearer" else None)
        try:
            key = self.state.secrets.get(SecretName.API_KEY)
        except SecretsError:
            key = None
        if (
            key
            and supplied
            and len(authorization) <= 1
            and len(api_keys) <= 1
            and all(
                v is not None and hmac.compare_digest(v.encode(), key.encode()) for v in supplied
            )
        ):
            return
        auth = self.state.auth
        local = is_local_client(request.scope)
        if local and auth.check_cli_token(bearer(headers)):
            return
        # Browsers omit Origin on same-origin GETs but always send Sec-Fetch-Site.
        origin = headers.get("origin")
        same_origin_page = headers.get("sec-fetch-site") == "same-origin" and (
            origin is None or is_same_origin(origin, headers.get("host"))
        )
        cookie = cookie_parser(headers.get("cookie", "")).get(SESSION_COOKIE)
        if cookie and same_origin_page and auth.verify_session(cookie):
            return
        # No local trust. Before sign-in became the default, a same-origin page on this Mac was
        # accepted
        # here while admin sign-in was off; a local process can forge those headers.
        raise ProxyError(401, "invalid or missing API key", "authentication_error")

    @staticmethod
    def cors_headers(allow_origin: str | None) -> dict[str, str]:
        if allow_origin is None:
            return {}
        headers = {
            "Access-Control-Allow-Origin": allow_origin,
            "Access-Control-Expose-Headers": f"Retry-After, WWW-Authenticate, {REQUEST_ID_HEADER}",
        }
        if allow_origin != p.ANY_ORIGIN:
            headers["Vary"] = "Origin"
        return headers

    def preflight(self, request: Request, methods: str) -> Response:
        try:
            allow = self.check(request, public=True)
        except ProxyError as error:
            return error.response()
        headers = {"Allow": methods, **self.cors_headers(allow)}
        if allow is not None and "access-control-request-method" in request.headers:
            headers["Access-Control-Allow-Methods"] = methods
            requested = request.headers.get("access-control-request-headers")
            if requested and requested.isprintable():
                headers["Access-Control-Allow-Headers"] = requested
            headers["Access-Control-Max-Age"] = "600"
        return Response(status_code=204, headers=headers)

    # Routing -------------------------------------------------------------

    def installed(self) -> frozenset[str]:
        return self.state.installed_models() or frozenset()

    def _profile_of(self, model: str, name: str) -> dict[str, Any] | None:
        profiles = effective_profiles(self.state.settings.current, model)
        profile = profiles.get(name)
        return dict(profile.overlay) if profile is not None else None

    def _overlay(self, model: str, profile: str | None) -> dict[str, Any]:
        defaults = sampling_defaults(self.state.settings.current, model)
        if profile is None:
            return defaults
        overlay = self._profile_of(model, profile) or {}
        return {**defaults, **overlay}

    def resolve(self, requested: str | None) -> tuple[str | None, str | None, bool]:
        """(real model, profile, active) for a requested name, without side effects.
        real model None = not a known name."""
        sup = self.sup
        active = sup.active_model() if sup else None
        names = set(sup.aliases()) if sup and sup.accepting else set()
        if active:
            names.add(active)
        if requested is None or requested == "":
            return None, None, False
        if requested in names:
            return active, None, True
        installed = self.installed()
        if requested in installed:
            return requested, None, False
        base, sep, profile = requested.rpartition(":")
        if sep and base:
            if base in names and active and self._profile_of(active, profile) is not None:
                return active, profile, True
            if base in installed and self._profile_of(base, profile) is not None:
                return base, profile, base == active
        return None, None, False

    async def route(self, request: Request, requested: Any) -> Route:
        sup = self.sup
        routing = self.state.settings.current.global_.routing
        name = requested if isinstance(requested, str) else None
        model, profile, is_active = self.resolve(name)
        if model is not None and is_active:
            await self._await_serving(model, routing.load_timeout)
            return Route(
                model=model,
                request_model=name,
                profile=profile,
                overlay=self._overlay(model, profile),
            )
        if model is None:
            fallback_ok = routing.unknown_model_fallback
            active = sup.active_model() if sup else None
            if name and not fallback_ok:
                raise self._not_found(name)
            if active is not None and (fallback_ok or not name):
                await self._await_serving(active, routing.load_timeout)
                return Route(
                    model=active,
                    request_model=name,
                    profile=None,
                    overlay=self._overlay(active, None),
                )
            default = routing.default_model
            if default and fallback_ok and default in self.installed() and routing.auto_load:
                model = default
            elif name:
                raise self._not_found(name)
            else:
                raise unavailable(
                    "No model is loaded. Load one in Splashboard or set a default model "
                    "(routing.default_model with routing.unknown_model_fallback)."
                )
        # An installed model that is not active (or the engine is stopped).
        if not routing.auto_load:
            if sup is not None and sup.active_model() is None:
                raise unavailable(f"No model is loaded; load {model} first (auto-load is off)")
            raise self._not_found(name or model)
        await self._switch(request, model, routing)
        return Route(
            model=model, request_model=name, profile=profile, overlay=self._overlay(model, profile)
        )

    def _not_found(self, name: str) -> ProxyError:
        installed = sorted(self.installed())
        return ProxyError(
            404,
            f"model {name} is not installed; installed models: "
            + (", ".join(installed) if installed else "none"),
            "model_not_found",
            details={"installed": installed},
        )

    async def _await_serving(self, model: str, timeout: float) -> None:
        sup = self.sup
        if sup is None:
            raise unavailable()
        if sup.accepting:
            return
        if (
            sup.state in ("starting", "crashed")
            and sup.model == model
            and await sup.wait_ready(timeout)
        ):
            return
        raise unavailable(f"Splash is {sup.state.replace('_', ' ')}; try again shortly")

    async def _switch(self, request: Request, model: str, routing: Any) -> None:
        sup = self.sup
        if sup is None:
            raise unavailable()
        wait = request.headers.get(SWITCH_HEADER, "").lower() == "wait" or (
            routing.switch_when_busy == "wait"
        )
        deadline = time.monotonic() + routing.load_timeout
        if sup.model == model and sup.state in ("starting", "crashed"):
            if await sup.wait_ready(routing.load_timeout):
                return
            raise unavailable(f"{model} did not become ready in {routing.load_timeout:g} s")
        if sup.active_model() is not None and sup.busy():
            if not wait:
                raise ProxyError(
                    503,
                    f"Splashboard is serving {sup.model}; switching models while requests are "
                    "in flight is disabled",
                    "model_switch_busy",
                    headers={"Retry-After": "10"},
                    details={"active": sup.model, "requested": model},
                )
            ok = await sup.wait_for(lambda: not sup.busy(), routing.load_timeout)
            if not ok:
                raise ProxyError(
                    503,
                    f"Splashboard is still serving {sup.model}",
                    "model_switch_busy",
                    headers={"Retry-After": "10"},
                )
        from ..errors import ApiError

        try:
            await sup.load(model, force=False, reason="auto_load")
        except ApiError as error:
            if error.code == "model_switch_busy":
                raise ProxyError(
                    503, error.message, "model_switch_busy", headers={"Retry-After": "10"}
                ) from None
            if error.code == "install_in_progress":
                # An auto-load never stops a download in progress; retry later.
                raise ProxyError(
                    503,
                    f"Splashboard is downloading files for {sup.model}; retry when it is ready",
                    "install_in_progress",
                    headers={"Retry-After": "30"},
                ) from None
            raise ProxyError(
                error.status if error.status != 422 else 503,
                error.message,
                error.code,
                headers={"Retry-After": "5"},
            ) from None
        remaining = max(1.0, deadline - time.monotonic())
        if not await sup.wait_ready(remaining):
            if sup.active_model() not in (None, model):
                raise ProxyError(
                    503,
                    f"Splashboard switched to {sup.active_model()} while loading {model}; retry",
                    "model_switch_busy",
                    headers={"Retry-After": "10"},
                )
            message = sup.error.message if sup.error else f"{model} is not ready"
            raise unavailable(f"Could not load {model}: {message}")

    # Forwarding ---------------------------------------------------------------------

    def _engine_headers(self, request: Request, key: str) -> dict[str, str]:
        out = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST_HEADERS}
        out["authorization"] = f"Bearer {key}"
        return out

    async def handle(
        self,
        request: Request,
        path: str,
        *,
        body_override: dict[str, Any] | None = None,
        rewrite_model: str | None = None,
        client_label: str | None = None,
        checked: bool = False,
        extra_overlay: dict[str, Any] | None = None,
    ) -> Response:
        """One generation-shaped request (`POST /v1/...`, `/tokenize`, `/apply-template`)."""
        anthropic = path.startswith("/v1/messages")
        systemone = path == "/v1/systemone"
        allow_origin: str | None = None
        cors: dict[str, str] = {}
        try:
            if not checked:
                allow_origin = self.check(request)
                cors = self.cors_headers(allow_origin)
            body = body_override if body_override is not None else await self._json_body(request)
            if rewrite_model is not None:
                body = {**body, "model": rewrite_model}
            route = await self.route(request, body.get("model"))
        except ProxyError as error:
            return error.response(anthropic, cors, systemone=systemone)
        if route.model is not None and self.sup.active_model() != route.model:
            # Another request switched the engine while this one waited for its
            # model: never forward to the wrong model.
            return ProxyError(
                503,
                f"Splashboard switched to {self.sup.active_model() or 'another model'} while "
                f"this request waited for {route.model}; retry",
                "model_switch_busy",
                headers={"Retry-After": "10"},
                details={"active": self.sup.active_model(), "requested": route.model},
            ).response(anthropic, cors, systemone=systemone)
        shape: Shape = SHAPES.get(path, "other")
        overlay = {**route.overlay, **(extra_overlay or {})}
        injected: dict[str, Any] = {}
        if shape in ("chat", "completions", "responses", "messages"):
            body, injected = inject(shape, body, overlay)
        requested = body.get("model")
        if route.model is not None and isinstance(requested, str):
            names = set(self.sup.aliases()) | {route.model}
            # "" was routed to the active model, but Splash would answer `model  not found`,
            # so it gets the name. A missing model stays missing: Splash accepts that.
            if not requested or route.profile is not None or requested not in names:
                body = {**body, "model": route.model}
        return await self._forward(
            request,
            "POST",
            path,
            body,
            route=route,
            shape=shape,
            injected=injected,
            cors=cors,
            anthropic=anthropic,
            client_label=client_label,
        )

    async def _json_body(self, request: Request) -> dict[str, Any]:
        raw = await request.body()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            # server/server.py _read_json_body (1.3.0) words both refusals so.
            raise ProxyError(
                400, "invalid JSON request body", "invalid_request_error", validation=True
            ) from None
        if not isinstance(data, dict):
            raise ProxyError(
                400, "request body must be an object", "invalid_request_error", validation=True
            )
        return data

    async def _forward(
        self,
        request: Request,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        *,
        route: Route | None,
        shape: Shape,
        injected: dict[str, Any],
        cors: dict[str, str],
        anthropic: bool,
        client_label: str | None = None,
        record: bool = True,
    ) -> Response:
        sup = self.sup
        systemone = path == "/v1/systemone"
        if sup is None or not sup.accepting or sup.base_url is None:
            return unavailable().response(anthropic, cors, systemone=systemone)
        request_id = "req_" + uuid.uuid4().hex[:24]
        headers = self._engine_headers(request, sup.internal_key)
        content = None
        if body is not None:
            content = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
            headers["content-type"] = "application/json"
        stream = bool(body.get("stream")) if body else False
        capture = UsageCapture(shape=shape, endpoint=path, stream=stream)
        started = time.monotonic()
        sup.request_started()
        finished = False

        def done() -> None:
            nonlocal finished
            if not finished:
                finished = True
                sup.request_finished()

        try:
            upstream = await self.client.send(
                self.client.build_request(
                    method,
                    sup.base_url + path,
                    headers=headers,
                    content=content,
                    params=dict(request.query_params) or None,
                ),
                stream=True,
            )
        except httpx.HTTPError as error:
            done()
            log.warning("engine request failed: %s", error)
            failure = unavailable(f"The engine did not answer: {error}")
            if record:
                self._record(
                    request,
                    route,
                    path,
                    stream,
                    failure.status,
                    failure.code,
                    capture,
                    started,
                    injected,
                    body,
                    request_id,
                    client_label,
                )
            return failure.response(anthropic, cors, systemone=systemone)
        out_headers = {
            k: v for k, v in upstream.headers.items() if k.lower() in FORWARD_RESPONSE_HEADERS
        }
        out_headers.update(cors)
        out_headers[REQUEST_ID_HEADER] = request_id
        content_type = upstream.headers.get("content-type", "")
        capture.stream = "text/event-stream" in content_type

        async def relay() -> AsyncIterator[bytes]:
            outcome = "cancelled"  # until the upstream body has been read to its end
            try:
                async for chunk in upstream.aiter_raw():
                    capture.feed(chunk)
                    yield chunk
                outcome = "complete"
            except httpx.HTTPError as error:
                # The engine dropped the connection mid-response (a crash or a kill).
                outcome = "engine_disconnected"
                log.warning("engine connection lost mid-response: %s", error)
                raise  # passed through as a disconnect, as Splash produced it
            finally:
                await upstream.aclose()
                done()
                capture.finish()
                if record:
                    status = upstream.status_code
                    code = capture.error_code
                    if outcome == "cancelled":
                        # The client went away first ("cancelled" status).
                        status, code = CANCELLED_STATUS, CANCELLED
                    elif outcome == "engine_disconnected" and code is None:
                        code = "engine_disconnected"
                    self._record(
                        request,
                        route,
                        path,
                        stream,
                        status,
                        code,
                        capture,
                        started,
                        injected,
                        body,
                        request_id,
                        client_label,
                    )
                    self._alerts(status, code, capture.error_message)

        if capture.stream:
            out_headers.setdefault("cache-control", "no-cache")
            out_headers["x-accel-buffering"] = "no"
        return StreamingResponse(relay(), status_code=upstream.status_code, headers=out_headers)

    async def passthrough(
        self, request: Request, method: str, path: str, *, public: bool = False
    ) -> Response:
        """GET/DELETE routes forwarded as they are (`/v1/responses/{id}`, `/status`, …)."""
        anthropic = path.startswith("/v1/messages")
        try:
            allow = self.check(request, public=public)
        except ProxyError as error:
            return error.response(anthropic)
        cors = self.cors_headers(allow)
        return await self._forward(
            request,
            method,
            path,
            None,
            route=None,
            shape="other",
            injected={},
            cors=cors,
            anthropic=anthropic,
            record=False,
        )

    async def raw(self, request: Request, method: str, path: str) -> Response:
        """Playground "raw to engine" (`/api/admin/engine/raw/{path}`): no injection or
        usage capture, the internal key still added; the response streams back."""
        sup = self.sup
        if sup is None or not sup.accepting or sup.base_url is None:
            from ..errors import ApiError

            raise ApiError(
                503, "The engine is not running", "engine_unavailable", headers={"Retry-After": "5"}
            )
        content = await request.body()
        headers = self._engine_headers(request, sup.internal_key)
        try:
            upstream = await self.client.send(
                self.client.build_request(
                    method,
                    f"{sup.base_url}/{path.lstrip('/')}",
                    headers=headers,
                    content=content or None,
                    params=dict(request.query_params) or None,
                ),
                stream=True,
            )
        except httpx.HTTPError as error:
            from ..errors import ApiError

            raise ApiError(
                503,
                f"The engine did not answer: {error}",
                "engine_unavailable",
                headers={"Retry-After": "5"},
            ) from None
        out_headers = {
            k: v for k, v in upstream.headers.items() if k.lower() in FORWARD_RESPONSE_HEADERS
        }

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(relay(), status_code=upstream.status_code, headers=out_headers)

    # Usage rows and alerts -----------------------------------------------------------

    def _record(
        self,
        request: Request,
        route: Route | None,
        path: str,
        stream: bool,
        status: int,
        code: str | None,
        capture: UsageCapture,
        started: float,
        injected: dict[str, Any],
        body: dict[str, Any] | None,
        request_id: str,
        client_label: str | None,
    ) -> None:
        same_origin = is_same_origin(request.headers.get("origin"), request.headers.get("host"))
        priority = body.get("priority") if body else None
        try:
            self.state.usage.insert_request(
                {
                    "ts": iso(),
                    "model": route.model if route else None,
                    "profile": route.profile if route else None,
                    "endpoint": path,
                    "client": client_label
                    or guess_client(request.headers.get("user-agent"), same_origin),
                    "stream": stream,
                    "status": status,
                    "error_code": code,
                    "prompt_tokens": capture.prompt_tokens,
                    "cached_tokens": capture.cached_tokens,
                    "completion_tokens": capture.completion_tokens,
                    "ttft_ms": capture.ttft_ms,
                    "prompt_ms": capture.prompt_ms,
                    "predicted_ms": capture.predicted_ms,
                    "duration_ms": (time.monotonic() - started) * 1000,
                    "priority": priority if isinstance(priority, str) else None,
                    "injected": injected or None,
                    "request_id": request_id,
                }
            )
        except Exception:
            log.exception("could not record usage")
        self.state.events.publish(
            "usage.request",
            {"request_id": request_id, "status": status, "model": route.model if route else None},
        )

    def _alerts(self, status: int, code: str | None, message: str | None = None) -> None:
        alerts = self.state.alerts
        # Splash uses `frontend_overloaded` for several capacities. The --queue-size
        # limit is the HTTP request gate ("frontend request capacity is exhausted",
        # server/server.py) and its native twin ("request queue is full",
        # server/backend.py); body, preparation and image capacity are not.
        # The message also identifies it on /v1/messages, which has no code.
        text = message or ""
        if status in (503, 529) and (
            "frontend request capacity is exhausted" in text or "request queue is full" in text
        ):
            size = self.state.settings.current.global_.serve.queue_size
            alerts.raise_alert(
                "queue_full",
                f"Queue full ({size}) — raise Queue size",
                "Splash refused a request because its queue is full (--queue-size).",
                source="proxy",
            )
            return
        entry = ERROR_ALERTS.get(code or "")
        if entry is not None:
            condition, title, message = entry
            actions = []
            if condition == "engine_failed":
                from ..events.alerts import RESTART_ACTION

                actions = [RESTART_ACTION]
            alerts.raise_alert(
                condition,  # type: ignore[arg-type]
                title,
                message,
                source="proxy",
                actions=actions,
            )

    # /v1/models ------------------------------------------------------------

    def models_list(self) -> dict[str, Any]:
        sup = self.sup
        data: list[dict[str, Any]] = []
        active_ids: set[str] = set()
        if sup is not None and sup.accepting and sup.engine_models:
            real = sup.model
            for entry in sup.engine_models:
                item = {**entry, "loaded": True}
                data.append(item)
                active_ids.add(str(entry.get("id")))
            base = next((e for e in sup.engine_models if e.get("id") == real), sup.engine_models[0])
            if real:
                for name in effective_profiles(self.state.settings.current, real):
                    if name == "default":
                        continue
                    item = {k: v for k, v in base.items() if k != "root"}
                    item.update(id=f"{real}:{name}", root=real, loaded=True, profile=name)
                    data.append(item)
        routing = self.state.settings.current.global_.routing
        if routing.auto_load:
            for model in sorted(self.installed()):
                if model in active_ids or (
                    sup is not None and model == sup.active_model() and sup.accepting
                ):
                    continue
                data.append(self._unloaded_entry(model))
        typed = [
            {
                "name": item["id"],
                "description": "Splash resident model"
                if item.get("loaded")
                else "Splash model (loads on request)",
                "release_date": "",
            }
            for item in data
        ]
        return {"object": "list", "data": data, "models": typed}

    def _unloaded_entry(self, model: str) -> dict[str, Any]:
        facts = self.state.usage.model_facts(model) or {}
        info = self.state.model_info(model) or {}
        context = facts.get("max_context") or DEFAULT_CONTEXT
        vision = facts.get("vision")
        if vision is None:
            vision = info.get("vision")
        if vision is None:
            vision = not info.get("language_only", False)
        return {
            "id": model,
            "object": "model",
            "created": 0,
            "owned_by": "splash",
            "max_model_len": context,
            "context_length": context,
            "vision": bool(vision),
            "input_modalities": ["text", "image", "pdf"] if vision else ["text"],
            "loaded": False,
        }

    async def models_response(self, request: Request, model_id: str | None = None) -> Response:
        try:
            allow = self.check(request)
        except ProxyError as error:
            return error.response()
        cors = self.cors_headers(allow)
        cors["x-typesafe-request-id"] = "req_" + uuid.uuid4().hex[:24]
        listing = self.models_list()
        if model_id is None:
            return JSONResponse(listing, headers=cors)
        entry = next((m for m in listing["data"] if m["id"] == model_id), None)
        if entry is None:
            return ProxyError(404, "model not found", "model_not_found").response(extra=cors)
        return JSONResponse(entry, headers=cors)

    # /ready, /status, /metrics -------------------------------------------------------

    async def ready(self, request: Request) -> Response:
        try:
            allow = self.check(request, public=True)
        except ProxyError as error:
            return error.response()
        cors = self.cors_headers(allow)
        sup = self.sup
        if sup is None or not sup.accepting or sup.base_url is None:
            return JSONResponse(
                {"status": "unavailable"}, status_code=503, headers={**cors, "Retry-After": "5"}
            )
        try:
            upstream = await self.client.get(
                sup.base_url + "/ready",
                headers={"Authorization": f"Bearer {sup.internal_key}"},
                timeout=5.0,
            )
        except httpx.HTTPError:
            return JSONResponse(
                {"status": "unavailable"}, status_code=503, headers={**cors, "Retry-After": "5"}
            )
        # A 503 here is not memory evidence: Splash answers it during transport recovery too
        # (server/backend.py is_ready). memory_critical comes from /status.
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type="application/json",
            headers=cors,
        )

    async def metrics(self, request: Request) -> Response:
        try:
            allow = self.check(request)
        except ProxyError as error:
            return error.response()
        cors = self.cors_headers(allow)
        text = ""
        sup = self.sup
        if sup is not None and sup.accepting and sup.base_url is not None:
            with contextlib.suppress(httpx.HTTPError):
                upstream = await self.client.get(
                    sup.base_url + "/metrics",
                    headers={"Authorization": f"Bearer {sup.internal_key}"},
                    timeout=10.0,
                )
                if upstream.status_code == 200:
                    text = upstream.text
        if text and not text.endswith("\n"):
            text += "\n"
        text += self.manager_gauges()
        return Response(text, media_type="text/plain; version=0.0.4; charset=utf-8", headers=cors)

    def manager_gauges(self) -> str:
        from .. import __version__

        sup = self.sup
        lines = [
            "# HELP splash_gui_up Splashboard manager is running.",
            "# TYPE splash_gui_up gauge",
            "splash_gui_up 1",
            "# HELP splash_gui_info Splashboard and engine versions.",
            "# TYPE splash_gui_info gauge",
        ]
        engine = self.state.engine_cached()
        lines.append(
            f'splash_gui_info{{version="{__version__}",engine_version="{engine.version or ""}"}} 1'
        )
        lines += [
            "# HELP splash_gui_engine_state Current engine state (1 for the active state).",
            "# TYPE splash_gui_engine_state gauge",
        ]
        current = sup.state if sup else "stopped"
        for name in (
            "stopped",
            "starting",
            "ready",
            "busy",
            "idle_released",
            "recovering",
            "engine_failed",
            "stopping",
            "crashed",
            "failed",
        ):
            lines.append(f'splash_gui_engine_state{{state="{name}"}} {1 if name == current else 0}')
        uptime = sup.view().uptime_s if sup else None
        lines += [
            "# HELP splash_gui_requests_in_flight Requests the manager is forwarding now.",
            "# TYPE splash_gui_requests_in_flight gauge",
            f"splash_gui_requests_in_flight {sup.in_flight if sup else 0}",
            "# HELP splash_gui_engine_uptime_seconds Seconds since the engine process started.",
            "# TYPE splash_gui_engine_uptime_seconds gauge",
            f"splash_gui_engine_uptime_seconds {uptime or 0}",
            "# HELP splash_gui_engine_restarts_detected_total Engine restarts seen in /status.",
            "# TYPE splash_gui_engine_restarts_detected_total counter",
            f"splash_gui_engine_restarts_detected_total {sup.restarts_detected if sup else 0}",
            "# HELP splash_gui_alerts_active Active health alerts.",
            "# TYPE splash_gui_alerts_active gauge",
            f"splash_gui_alerts_active {len(self.state.alerts.all())}",
        ]
        downloads = self.state.downloads
        active = 0
        if downloads is not None:
            with contextlib.suppress(Exception):
                active = sum(
                    1 for d in downloads.items.values() if d.state in ("running", "verifying")
                )
        lines += [
            "# HELP splash_gui_downloads_active Downloads running or verifying.",
            "# TYPE splash_gui_downloads_active gauge",
            f"splash_gui_downloads_active {active}",
        ]
        return "\n".join(lines) + "\n"

    async def status(self, request: Request) -> Response:
        try:
            allow = self.check(request)
        except ProxyError as error:
            return error.response()
        cors = self.cors_headers(allow)
        sup = self.sup
        if sup is None or not sup.accepting:
            return unavailable().response(extra=cors)
        return await self._forward(
            request,
            "GET",
            "/status",
            None,
            route=None,
            shape="other",
            injected={},
            cors=cors,
            anthropic=False,
            record=False,
        )

"""Host allowlist, admin CSRF and admin auth on the public port (SPEC §7.2, §14, §17.1).

A pure ASGI middleware, so streaming (SSE) responses pass through unbuffered.

- **Host** (`/`, `/admin*`, `/api/admin*`): as Splash's `validate_headers`, only
  loopback names, the bind address, the socket's own address and
  `server.allowed_hosts` are served. This keeps DNS-rebound pages out of the admin.
- **No local trust** (D58): every `/api/admin/*` request needs a valid session cookie
  or the CLI token, except `/auth/state|login|logout|exchange`. With
  `security.admin_requires_key` turned off, clients on this Mac may still *read*
  without one, but every write and every secret read needs one. Being on loopback
  is never a credential.
- **CSRF** (mutating `/api/admin/*` from a browser): a same-origin `Origin` **and**
  `Sec-Fetch-Site: same-origin`, or the CLI token. A non-browser client can forge
  both, so this is an extra layer for cookie writes, never enough on its own.
- The SPA's static files stay public so the login page can load; they hold no data.

`check_host` and `allowed_hosts` are reused by the proxy for `/v1/*`.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Any, Literal

from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Receive, Scope, Send

from ..errors import ApiError
from ..settings import parsers as p
from ..settings.store import SettingsStore
from .core import SESSION_COOKIE, AuthManager

ADMIN_API = "/api/admin"
AUTH_EXEMPT = frozenset(
    f"{ADMIN_API}/auth/{name}" for name in ("state", "login", "logout", "exchange")
)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# D58: reads that run programs moved from GET to POST so a cross-site <img> or link
# cannot trigger them. They stay reads: with sign-in off they need no credential.
# Only fixed diagnostics belong here. Anything that starts, stops, creates, writes or
# runs a command the user configured (e.g. `POST /mcp/tools`, which starts MCP
# servers) is a write and always needs a session or the CLI token.
READ_ONLY_POSTS = frozenset(
    f"{ADMIN_API}/{name}"
    for name in (
        "doctor",
        "diagnostics",
        "system",
        "system/brew",
        "integrations",
        "benchmark/preflight",
        "inspect",
    )
)
SECRET_READS = (f"{ADMIN_API}/settings/secrets/",)
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")
WILDCARD_BINDS = ("0.0.0.0", "::")  # noqa: S104

AuthMethod = Literal["session", "cli_token", "open"]


def allowed_hosts(bind_host: str, extra: Iterable[str], local_address: str | None) -> set[str]:
    """The Host names the public port serves (server.py `allowed_hosts` in Splash)."""
    names = {
        host.lower().rstrip(".")
        for host in (*extra, bind_host, *LOOPBACK_NAMES)
        if host not in WILDCARD_BINDS
    }
    if local_address:
        names.add(local_address.lower())
    return names


def check_host(host_header: list[str], allowed: set[str]) -> str:
    """The Host's name, or ApiError 403 naming the setting that would allow it."""
    if len(host_header) != 1:
        raise ApiError(403, "expected one Host header", "host_not_allowed")
    try:
        host, _ = p.parse_authority(host_header[0])
    except ValueError:
        raise ApiError(403, "invalid Host header", "host_not_allowed") from None
    if host not in allowed:
        raise ApiError(
            403,
            f"Host {host} is not allowed; add it to Settings → Server & network → "
            "Allowed hosts (server.allowed_hosts) to accept it",
            "host_not_allowed",
            details={"host": host, "setting": "server.allowed_hosts"},
        )
    return host


def is_same_origin(origin: str | None, host_header: str | None) -> bool:
    """Whether `Origin` names the page's own server, as Splash compares them."""
    if not origin or not host_header:
        return False
    try:
        scheme, origin_host, origin_port = p.parse_origin(origin)
        host, port = p.parse_authority(host_header)
    except ValueError:
        return False
    default = p.DEFAULT_PORTS.get(scheme)
    return default is not None and (origin_host, origin_port) == (
        host,
        default if port is None else port,
    )


def is_loopback_client(client: Any) -> bool:
    if not client:
        return False
    try:
        return ipaddress.ip_address(str(client[0]).split("%")[0]).is_loopback
    except ValueError:
        return False


def is_local_client(scope: Scope) -> bool:
    """A client on this Mac: loopback, or the address the connection arrived on.

    With `server.host` set to one LAN address, the menu bar app and the CLI shim
    connect to that address, so their peer address is the Mac's own, not loopback.
    A remote host cannot complete a TCP handshake from our own address.
    """
    if is_loopback_client(scope.get("client")):
        return True
    client, server = scope.get("client"), scope.get("server")
    if not client or not server:
        return False
    try:
        peer = ipaddress.ip_address(str(client[0]).split("%")[0])
        local = ipaddress.ip_address(str(server[0]).split("%")[0])
    except ValueError:
        return False
    return peer == local and not local.is_unspecified


def bearer(headers: Headers) -> str | None:
    values = headers.getlist("authorization")
    if len(values) != 1:
        return None
    scheme, _, value = values[0].partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def is_read_only_post(path: str) -> bool:
    if path in READ_ONLY_POSTS:
        return True
    # /integrations/{client}/print
    prefix = f"{ADMIN_API}/integrations/"
    return path.startswith(prefix) and path.endswith("/print") and path.count("/") == 5


def is_secret_read(path: str) -> bool:
    return path.startswith(SECRET_READS)


def _guarded(path: str) -> bool:
    return (
        path == "/"
        or path == "/admin"
        or path.startswith("/admin/")
        or path == ADMIN_API
        or path.startswith(f"{ADMIN_API}/")
    )


class AdminGuard:
    def __init__(self, app: ASGIApp, *, settings: SettingsStore, auth: AuthManager) -> None:
        self.app = app
        self.settings = settings
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _guarded(scope["path"]):
            await self.app(scope, receive, send)
            return
        try:
            method = self.authorize(scope)
        except ApiError as error:
            await error.response()(scope, receive, send)
            return
        scope.setdefault("state", {})["auth_method"] = method
        await self.app(scope, receive, send)

    def authorize(self, scope: Scope) -> AuthMethod | None:
        """Raise ApiError for a refused request; return how it authenticated."""
        headers = Headers(scope=scope)
        g = self.settings.current.global_
        server = scope.get("server")
        local = str(server[0]) if server else None
        allowed = allowed_hosts(g.server.host, g.server.allowed_hosts, local)
        check_host(headers.getlist("host"), allowed)

        path: str = scope["path"]
        if not (path == ADMIN_API or path.startswith(f"{ADMIN_API}/")):
            return None
        if is_local_client(scope) and self.auth.check_cli_token(bearer(headers)):
            return "cli_token"
        method = scope["method"]
        if method not in SAFE_METHODS:
            same_origin = is_same_origin(headers.get("origin"), headers.get("host"))
            if not same_origin or headers.get("sec-fetch-site") != "same-origin":
                raise ApiError(
                    403,
                    "Cross-site request refused: admin changes need a same-origin page "
                    "or the CLI token",
                    "csrf_refused",
                )
        cookie = cookie_parser(headers.get("cookie", "")).get(SESSION_COOKIE)
        if self.auth.verify_session(cookie):
            return "session"
        if path in AUTH_EXEMPT:
            return None
        if g.security.admin_requires_key:
            raise ApiError(
                401,
                "Sign in with the API key to use the admin",
                "auth_required",
                details={"admin_requires_key": True},
            )
        if not is_local_client(scope):
            # With admin sign-in off the admin reads are open to this Mac only. On a
            # LAN bind (which D42 refuses anyway) other machines get nothing.
            raise ApiError(
                403,
                "The admin is reachable from other machines only with Settings → "
                "Security → Protect the admin "
                "(security.admin_requires_key)",
                "admin_remote_refused",
                details={"setting": "security.admin_requires_key"},
            )
        write = method not in SAFE_METHODS and not is_read_only_post(path)
        if write or is_secret_read(path):
            # D58: Origin and Sec-Fetch-Site are trivially forged by a local process,
            # so loopback alone never changes settings, reads a key or starts an MCP
            # server (which runs code as the user).
            raise ApiError(
                401,
                "Sign in to make changes or read secrets; admin sign-in is off, so this "
                "Mac can only read",
                "auth_required",
                details={"admin_requires_key": False},
            )
        return "open"

"""Response security headers and a request body cap, as one pure ASGI middleware.

Pure ASGI (not BaseHTTPMiddleware) so SSE and streamed proxy responses pass through
unbuffered.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any

# Chat attachments stream with their own 64 MiB cap; JSON and proxy bodies are far smaller.
MAX_BODY = 80 * 1024 * 1024

BASE_HEADERS = [
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
]
# For responses that set no policy of their own (JSON, files): nothing may frame them.
DEFAULT_CSP = b"frame-ancestors 'none'; object-src 'none'; base-uri 'none'"

_INLINE_SCRIPT = re.compile(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
_csp_cache: dict[tuple[str, int], str] = {}


def spa_csp(index: Path) -> str:
    """The admin page's policy: its own scripts, plus the inline theme script by hash."""
    try:
        info = index.stat()
    except OSError:
        info = None
    key = (str(index), info.st_mtime_ns if info else 0)
    if key not in _csp_cache:
        try:
            html = index.read_bytes()
        except OSError:
            html = b""
        hashes = " ".join(
            "'sha256-" + base64.b64encode(hashlib.sha256(body).digest()).decode() + "'"
            for body in _INLINE_SCRIPT.findall(html)
        )
        _csp_cache.clear()
        _csp_cache[key] = (
            f"default-src 'self'; script-src 'self' {hashes}".rstrip()
            + "; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
    return _csp_cache[key]


class Hardening:
    def __init__(self, app: Any, max_body: int = MAX_BODY) -> None:
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_with_headers(message: Any) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = list(message.get("headers", []))
                names = {name.lower() for name, _ in headers}
                headers += [(n, v) for n, v in BASE_HEADERS if n not in names]
                if b"content-security-policy" not in names:
                    headers.append((b"content-security-policy", DEFAULT_CSP))
                message = {**message, "headers": headers}
            await send(message)

        async def too_large() -> None:
            body = (
                b'{"error":{"message":"request body is too large",'
                b'"type":"invalid_request_error","code":"body_too_large"}}'
            )
            await send_with_headers(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.max_body:
                        await too_large()
                        return
                except ValueError:
                    pass
        received = 0
        overflow = False

        async def capped_receive() -> Any:
            nonlocal received, overflow
            if overflow:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    overflow = True
                    return {"type": "http.disconnect"}
            return message

        try:
            await self.app(scope, capped_receive, send_with_headers)
        except Exception:
            if not overflow:
                raise
        if overflow and not started:
            await too_large()

"""Seam for the public API proxy (SPEC §7): `/v1/*`, `/ready`, `/status`, `/metrics`,
`/tokenize`, `/apply-template` and `/api/codex/v1/*`.

The proxy phase adds its routes to `router`; `app.create_app` mounts it at the root
before the SPA routes. Keep `/health`, `/` and `/admin` out of it (app.py owns them).
The admin guard checks Host only for `/`, `/admin*` and `/api/admin/*`; the proxy applies
Splash-equivalent Host/Origin/API-key checks to its own routes, reusing
`auth.guard.allowed_hosts` and `auth.guard.check_host`.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

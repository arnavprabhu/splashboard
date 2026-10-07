"""Admin auth, CSRF and the Host allowlist (SPEC §8.2, §14, §17.1, D58)."""

from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.auth.core import LINK_TTL_S, SESSION_COOKIE, SESSION_TTL_S, AuthManager
from splash_gui.auth.guard import allowed_hosts, check_host, is_same_origin
from splash_gui.errors import ApiError
from splash_gui.paths import Paths
from splash_gui.secrets import MemoryBackend, SecretName, SecretStore

from .conftest import LOOPBACK_CLIENT, mode

ORIGIN = "http://127.0.0.1:8000"


def _require_admin_key(client: TestClient) -> str:
    """Turn on admin auth through the API (as the CLI would) and return the key."""
    key = client.post("/api/admin/settings/secrets/api-key").json()["key"]
    doc = client.get("/api/admin/settings").json()["settings"]
    doc["global"]["security"]["admin_requires_key"] = True
    assert client.put("/api/admin/settings", json=doc).status_code == 200
    return str(key)


def _sign_in_off(client: TestClient) -> None:
    doc = client.get("/api/admin/settings").json()["settings"]
    doc["global"]["security"]["admin_requires_key"] = False
    assert client.put("/api/admin/settings", json=doc).status_code == 200


def _login(browser: TestClient, client: TestClient) -> None:
    key = client.get("/api/admin/settings/secrets/api-key").json()["key"]
    assert browser.post("/api/admin/auth/login", json={"key": key}).status_code == 200


# CLI token -------------------------------------------------------------------


def test_cli_token_file_is_private_and_stable(paths: Paths) -> None:
    auth = AuthManager(paths, SecretStore(MemoryBackend()))
    token = auth.ensure_cli_token()
    assert mode(paths.cli_token) == 0o600
    assert len(token) >= 32
    assert AuthManager(paths, SecretStore(MemoryBackend())).ensure_cli_token() == token


def test_cli_token_repaired_when_short(paths: Paths) -> None:
    paths.cli_token.write_text("x\n")
    paths.cli_token.chmod(0o644)
    token = AuthManager(paths, SecretStore(MemoryBackend())).ensure_cli_token()
    assert token != "x" and mode(paths.cli_token) == 0o600


def test_app_creates_cli_token(app: FastAPI, paths: Paths) -> None:
    assert paths.cli_token.read_text().strip() == app.state.manager.auth.cli_token()


# Sessions ----------------------------------------------------------------------


@pytest.fixture
def auth(paths: Paths) -> AuthManager:
    secrets = SecretStore(MemoryBackend())
    secrets.set(SecretName.API_KEY, "sk-splash-correct-horse")
    return AuthManager(paths, secrets)


def test_session_roundtrip_and_expiry(auth: AuthManager) -> None:
    cookie = auth.issue_session(now=1000.0)
    assert auth.verify_session(cookie, now=1001.0)
    assert not auth.verify_session(cookie, now=1000.0 + SESSION_TTL_S + 1)


def test_session_tampering_refused(auth: AuthManager) -> None:
    cookie = auth.issue_session()
    version, expiry, nonce, signature = cookie.split(".")
    assert not auth.verify_session(f"{version}.{int(expiry) + 999}.{nonce}.{signature}")
    assert not auth.verify_session(cookie[:-2] + "AA")
    assert not auth.verify_session("garbage")
    assert not auth.verify_session(None)


def test_rotating_the_key_ends_sessions(auth: AuthManager) -> None:
    cookie = auth.issue_session()
    auth.secrets.set(SecretName.API_KEY, "sk-splash-another-key")
    assert not auth.verify_session(cookie)


def test_sessions_survive_a_manager_restart(auth: AuthManager, paths: Paths) -> None:
    cookie = auth.issue_session()
    restarted = AuthManager(paths, auth.secrets)  # same Keychain, new process
    assert restarted.verify_session(cookie)


def test_revoked_session_refused(auth: AuthManager) -> None:
    cookie = auth.issue_session()
    auth.revoke_session(cookie)
    assert not auth.verify_session(cookie)


def test_login_throttle(auth: AuthManager) -> None:
    for i in range(10):
        assert auth.login_allowed("10.0.0.2", now=float(i))
        auth.record_login_failure("10.0.0.2", now=float(i))
    assert not auth.login_allowed("10.0.0.2", now=10.0)
    assert auth.login_allowed("10.0.0.3", now=10.0), "other clients are not locked out"
    assert auth.login_allowed("10.0.0.2", now=200.0)


# Host allowlist --------------------------------------------------------------


def test_allowed_hosts_matches_splash() -> None:
    names = allowed_hosts("0.0.0.0", ["MyMac.local."], "192.168.1.5")  # noqa: S104
    assert names == {"localhost", "127.0.0.1", "::1", "mymac.local", "192.168.1.5"}
    assert "10.0.0.2" in allowed_hosts("10.0.0.2", [], None)


@pytest.mark.parametrize("host", ["localhost:8000", "127.0.0.1", "[::1]:8000", "LOCALHOST."])
def test_check_host_accepts_loopback(host: str) -> None:
    check_host([host], allowed_hosts("127.0.0.1", [], None))


@pytest.mark.parametrize("hosts", [["evil.example:8000"], [], ["a", "b"], ["bad host"]])
def test_check_host_refuses(hosts: list[str]) -> None:
    with pytest.raises(ApiError) as caught:
        check_host(hosts, allowed_hosts("127.0.0.1", [], None))
    assert caught.value.status == 403 and caught.value.code == "host_not_allowed"


def test_dns_rebinding_refused(app: FastAPI) -> None:
    rebound = TestClient(app, client=LOOPBACK_CLIENT)
    for path in ("/api/admin/settings", "/admin/", "/"):
        response = rebound.get(path, headers={"Host": "attacker.example:8000"})
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "host_not_allowed"
        assert "server.allowed_hosts" in response.json()["error"]["message"]


def test_health_is_not_host_checked(app: FastAPI) -> None:
    other = TestClient(app, client=LOOPBACK_CLIENT)
    assert other.get("/health", headers={"Host": "anything.example"}).status_code == 200


def test_allowed_host_setting_admits(client: TestClient, app: FastAPI) -> None:
    doc = client.get("/api/admin/settings").json()["settings"]
    doc["global"]["server"]["allowed_hosts"] = ["mymac.local"]
    assert client.put("/api/admin/settings", json=doc).status_code == 200
    lan = TestClient(app, client=LOOPBACK_CLIENT)
    assert lan.get("/api/admin/auth/state", headers={"Host": "mymac.local:8000"}).status_code == 200


# CSRF ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "host", "same"),
    [
        ("http://127.0.0.1:8000", "127.0.0.1:8000", True),
        ("http://localhost", "localhost:80", True),
        ("http://localhost:8000", "127.0.0.1:8000", False),
        ("https://127.0.0.1:8000", "127.0.0.1:8000", True),
        ("http://127.0.0.1:9000", "127.0.0.1:8000", False),
        ("null", "127.0.0.1:8000", False),
        (None, "127.0.0.1:8000", False),
        ("tauri://localhost", "localhost", False),
    ],
)
def test_same_origin(origin: str | None, host: str, same: bool) -> None:
    assert is_same_origin(origin, host) is same


def test_reads_need_a_credential_by_default(app: FastAPI) -> None:
    plain = TestClient(app, client=LOOPBACK_CLIENT)
    refused = plain.get("/api/admin/settings")
    assert refused.status_code == 401
    assert refused.json()["error"] == {
        "message": "Sign in with the API key to use the admin",
        "type": "authentication_error",
        "code": "auth_required",
        "details": {"admin_requires_key": True},
    }


def test_reads_need_no_origin_with_sign_in_off(app: FastAPI, client: TestClient) -> None:
    _sign_in_off(client)
    plain = TestClient(app, client=LOOPBACK_CLIENT)
    assert plain.get("/api/admin/settings").status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://evil.example"},
        {"Origin": ORIGIN},
        {"Sec-Fetch-Site": "same-origin"},
        {"Origin": "http://evil.example", "Sec-Fetch-Site": "same-origin"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
    ],
)
def test_cross_site_writes_refused(app: FastAPI, headers: dict[str, str]) -> None:
    before = app.state.manager.secrets.get(SecretName.API_KEY)
    page = TestClient(app, base_url=ORIGIN, client=LOOPBACK_CLIENT)
    response = page.post("/api/admin/settings/secrets/api-key", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"] == {
        "message": "Cross-site request refused: admin changes need a same-origin page "
        "or the CLI token",
        "type": "permission_error",
        "code": "csrf_refused",
    }
    assert app.state.manager.secrets.get(SecretName.API_KEY) == before


def test_same_origin_page_needs_a_session_to_write(browser: TestClient, client: TestClient) -> None:
    for sign_in in (True, False):
        if not sign_in:
            _sign_in_off(client)
        refused = browser.post("/api/admin/settings/secrets/api-key")
        assert refused.status_code == 401, sign_in
        assert refused.json()["error"]["code"] == "auth_required"
        assert refused.json()["error"]["details"] == {"admin_requires_key": sign_in}
    _login(browser, client)
    assert browser.post("/api/admin/settings/secrets/api-key").status_code == 200


def test_cli_token_only_from_loopback(app: FastAPI) -> None:
    token = app.state.manager.auth.cli_token()
    remote = TestClient(
        app, client=("192.168.1.20", 50000), headers={"Authorization": f"Bearer {token}"}
    )
    assert remote.post("/api/admin/settings/secrets/api-key").status_code == 403
    wrong = TestClient(app, client=LOOPBACK_CLIENT, headers={"Authorization": "Bearer nope"})
    assert wrong.post("/api/admin/settings/secrets/api-key").status_code == 403


def test_open_admin_refuses_network_clients(app: FastAPI, client: TestClient) -> None:
    # A LAN bind needs an API key (SPEC §17.1). With admin auth off, a LAN machine
    # forging the CSRF headers must not be able to read that key or change settings.
    doc = client.get("/api/admin/settings").json()["settings"]
    doc["global"]["server"]["allowed_hosts"] = ["mymac.local"]
    doc["global"]["security"]["admin_requires_key"] = False
    assert client.put("/api/admin/settings", json=doc).status_code == 200
    lan = TestClient(
        app,
        base_url="http://mymac.local:8000",
        client=("192.168.1.20", 50000),
        headers={"Origin": "http://mymac.local:8000", "Sec-Fetch-Site": "same-origin"},
    )
    for response in (
        lan.get("/api/admin/settings/secrets/api-key"),
        lan.post("/api/admin/mcp/servers"),
        lan.get("/api/admin/settings"),
    ):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "admin_remote_refused"
    # The SPA itself (no data) still loads, so the page can explain the refusal.
    assert lan.get("/admin/").status_code == 200
    # With admin auth on, a LAN browser may log in as before.
    key = _require_admin_key(client)
    assert lan.post("/api/admin/auth/login", json={"key": key}).status_code == 200
    assert lan.get("/api/admin/settings").status_code == 200


# Admin auth ------------------------------------------------------------------------


def test_admin_auth_flow(client: TestClient, browser: TestClient) -> None:
    key = _require_admin_key(client)

    # Locked out without a session; the auth routes and the SPA stay reachable.
    response = browser.get("/api/admin/settings")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_required"
    assert browser.get("/api/admin/auth/state").json() == {
        "admin_requires_key": True,
        "authenticated": False,
        "method": None,
    }
    assert browser.get("/admin/").status_code == 200

    # Wrong key, then the right one.
    bad = browser.post("/api/admin/auth/login", json={"key": "sk-splash-wrong"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_key"
    good = browser.post("/api/admin/auth/login", json={"key": key})
    assert good.status_code == 200
    assert good.json() == {"admin_requires_key": True, "authenticated": True, "method": "session"}
    cookie = good.headers["set-cookie"].lower()
    for attribute in ("httponly", "samesite=strict", "path=/", f"max-age={SESSION_TTL_S}"):
        assert attribute in cookie
    assert browser.get("/api/admin/settings").status_code == 200
    assert browser.get("/api/admin/auth/state").json()["method"] == "session"

    # The CLI token works regardless of the session.
    assert client.get("/api/admin/settings").status_code == 200
    assert client.get("/api/admin/auth/state").json()["method"] == "cli_token"

    # Logout clears and revokes the session.
    session = browser.cookies.get(SESSION_COOKIE)
    assert browser.post("/api/admin/auth/logout").status_code == 204
    assert browser.get("/api/admin/settings").status_code == 401
    browser.cookies.set(SESSION_COOKIE, session or "")
    assert browser.get("/api/admin/settings").status_code == 401


def test_login_needs_same_origin(app: FastAPI, client: TestClient) -> None:
    key = _require_admin_key(client)
    page = TestClient(app, base_url=ORIGIN, client=LOOPBACK_CLIENT)
    response = page.post(
        "/api/admin/auth/login", json={"key": key}, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403


def test_login_rate_limited(client: TestClient, browser: TestClient) -> None:
    _require_admin_key(client)
    for _ in range(10):
        assert browser.post("/api/admin/auth/login", json={"key": "nope"}).status_code == 401
    response = browser.post("/api/admin/auth/login", json={"key": "nope"})
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"
    assert response.headers["retry-after"] == "60"


def _require_api_key(client: TestClient) -> None:
    client.post("/api/admin/settings/secrets/api-key")
    doc = client.get("/api/admin/settings").json()["settings"]
    doc["global"]["security"]["api_key_required"] = True
    assert client.put("/api/admin/settings", json=doc).status_code == 200


def test_v1_trusts_no_page_without_a_session(
    app: FastAPI, client: TestClient, browser: TestClient
) -> None:
    # D58: before, with admin sign-in off, a same-origin page on this Mac reached /v1
    # without the key. A local process can forge those headers, so it needs a session.
    _require_api_key(client)
    _sign_in_off(client)
    assert browser.get("/v1/models").status_code == 401
    forged = TestClient(
        app, base_url=ORIGIN, client=LOOPBACK_CLIENT, headers={"Sec-Fetch-Site": "same-origin"}
    )
    assert forged.get("/v1/models").status_code == 401
    _login(browser, client)
    assert browser.get("/v1/models").status_code == 200
    # A same-origin GET carries no Origin header, only Sec-Fetch-Site.
    session = browser.cookies.get(SESSION_COOKIE)
    plain_get = TestClient(
        app, base_url=ORIGIN, client=LOOPBACK_CLIENT, headers={"Sec-Fetch-Site": "same-origin"}
    )
    plain_get.cookies.set(SESSION_COOKIE, session or "")
    assert plain_get.get("/v1/models").status_code == 200
    evil = plain_get.get("/v1/models", headers={"Origin": "http://evil.example"})
    assert evil.status_code == 403


def test_cli_token_from_the_macs_own_lan_address(app: FastAPI) -> None:
    # server.host = 192.168.1.5: the menu bar app and the shim connect to that
    # address, so the peer is the Mac itself, not loopback.
    token = app.state.manager.auth.cli_token()
    own = TestClient(
        app,
        base_url="http://192.168.1.5:8000",
        client=("192.168.1.5", 50000),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert own.post("/api/admin/settings/secrets/api-key").status_code == 200
    other = TestClient(
        app,
        base_url="http://192.168.1.5:8000",
        client=("192.168.1.20", 50000),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert other.post("/api/admin/settings/secrets/api-key").status_code == 403


# D58: no local trust with sign-in off ------------------------------------------------


def test_sign_in_off_keeps_reads_open_but_not_secrets_or_writes(
    app: FastAPI, client: TestClient, browser: TestClient
) -> None:
    _sign_in_off(client)
    assert browser.get("/api/admin/auth/state").json() == {
        "admin_requires_key": False,
        "authenticated": False,
        "method": "open",
    }
    assert browser.get("/api/admin/settings").status_code == 200
    secret = browser.get("/api/admin/settings/secrets/api-key")
    assert secret.status_code == 401 and "sk-splash-" not in secret.text
    assert browser.put("/api/admin/mcp/servers", json={"servers": {}}).status_code == 401
    # Reads that moved to POST (they run programs) stay reads.
    assert browser.post("/api/admin/system").status_code == 200
    assert browser.post("/api/admin/benchmark/preflight").status_code == 200
    # The old GETs are gone.
    assert browser.get("/api/admin/doctor").status_code == 405
    # A signed-in page may do everything.
    _login(browser, client)
    assert browser.get("/api/admin/auth/state").json()["method"] == "session"
    assert browser.get("/api/admin/settings/secrets/api-key").json()["key"].startswith("sk-splash-")


# The documented read-only POSTs (docs/api.md §12.5). Adding one is a security decision:
# only fixed diagnostics, never a route that starts, writes or runs a configured command.
DOCUMENTED_READ_ONLY_POSTS = {
    "/doctor",
    "/diagnostics",
    "/system",
    "/system/brew",
    "/integrations",
    "/integrations/{name}/print",
    "/benchmark/preflight",
    "/inspect",
}


def _admin_routes(app: FastAPI) -> list[tuple[str, str]]:
    """(METHOD, path template) of every admin route, from the app's OpenAPI schema."""
    paths = app.openapi()["paths"]
    return sorted(
        (method.upper(), path)
        for path, operations in paths.items()
        if path.startswith("/api/admin/")
        for method in operations
    )


def test_every_admin_write_needs_a_credential_with_sign_in_off(
    app: FastAPI, client: TestClient, browser: TestClient
) -> None:
    """D58: with sign-in off, every non-GET admin route but the documented read-only
    POSTs and the auth routes answers 401 to a same-origin page with no session. Walks
    the app's own routes, so a new write route is covered without editing this test."""
    import re

    from splash_gui.auth.guard import AUTH_EXEMPT, is_read_only_post

    _sign_in_off(client)
    read_only, checked = set(), 0
    for method, template in _admin_routes(app):
        if method in ("GET", "HEAD"):
            continue
        path = re.sub(r"\{([^}:]+)(:[^}]*)?\}", "claude", template)
        if path in AUTH_EXEMPT:
            continue
        if is_read_only_post(path):
            assert method == "POST", (method, template)
            read_only.add(template.removeprefix("/api/admin"))
            continue
        response = browser.request(method, path, json={})
        assert response.status_code == 401, (method, template, response.text)
        assert response.json()["error"]["code"] == "auth_required", (method, template)
        checked += 1
    assert read_only == DOCUMENTED_READ_ONLY_POSTS
    assert checked > 60, "the walk found the admin routes"
    assert ("POST", "/api/admin/mcp/tools") in _admin_routes(app)
    assert ("GET", "/api/admin/mcp/tools") not in _admin_routes(app)


def test_read_only_posts_still_need_same_origin(app: FastAPI, client: TestClient) -> None:
    _sign_in_off(client)
    cross = TestClient(app, base_url=ORIGIN, client=LOOPBACK_CLIENT)
    response = cross.post("/api/admin/doctor", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "csrf_refused"


# D58: one-time login link -----------------------------------------------------------


def test_login_link_and_exchange(app: FastAPI, client: TestClient, browser: TestClient) -> None:
    link = client.post("/api/admin/auth/link")
    assert link.status_code == 200
    body = link.json()
    assert body["expires_in"] == LINK_TTL_S == 60
    assert body["url"].startswith("/admin/login?code=")
    code = body["url"].split("code=", 1)[1]
    assert len(code) >= 43

    # A plain (non-browser) client cannot spend it, even with the CLI token.
    assert client.post("/api/admin/auth/exchange", json={"code": code}).status_code == 403
    cross = TestClient(app, base_url=ORIGIN, client=LOOPBACK_CLIENT)
    assert cross.post("/api/admin/auth/exchange", json={"code": code}).status_code == 403

    assert browser.get("/api/admin/settings").status_code == 401
    good = browser.post("/api/admin/auth/exchange", json={"code": code})
    assert good.status_code == 200
    assert good.json() == {"admin_requires_key": True, "authenticated": True, "method": "session"}
    assert "httponly" in good.headers["set-cookie"].lower()
    assert browser.get("/api/admin/settings").status_code == 200

    # Single use.
    again = TestClient(
        app,
        base_url=ORIGIN,
        client=LOOPBACK_CLIENT,
        headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
    )
    reused = again.post("/api/admin/auth/exchange", json={"code": code})
    assert reused.status_code == 401
    assert reused.json()["error"]["code"] == "invalid_code"


def test_only_the_cli_token_mints_links(client: TestClient, browser: TestClient) -> None:
    assert browser.post("/api/admin/auth/link").status_code == 401
    _login(browser, client)
    refused = browser.post("/api/admin/auth/link")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "cli_token_required"


def test_codes_expire(auth: AuthManager) -> None:
    code = auth.issue_code(now=100.0)
    assert not auth.redeem_code(code, now=100.0 + LINK_TTL_S + 1)
    assert not auth.redeem_code(code, now=101.0), "an expired code is gone"
    fresh = auth.issue_code(now=200.0)
    assert auth.redeem_code(fresh, now=230.0)
    assert not auth.redeem_code("nonsense", now=230.0)


# D58: revocations survive a restart ------------------------------------------------


def test_revocations_persist_across_restarts(auth: AuthManager, paths: Paths) -> None:
    cookie = auth.issue_session()
    auth.revoke_session(cookie)
    assert mode(paths.revoked_sessions) == 0o600
    stored = json.loads(paths.revoked_sessions.read_text())
    assert stored["version"] == 1 and len(stored["revoked"]) == 1
    restarted = AuthManager(paths, auth.secrets)  # same Keychain, new process
    assert not restarted.verify_session(cookie)
    assert restarted.verify_session(restarted.issue_session())


def test_expired_revocations_are_pruned(auth: AuthManager, paths: Paths) -> None:
    now = time.time()
    paths.revoked_sessions.write_text(
        json.dumps({"version": 1, "revoked": {"old": now - 10, "live": now + 100}})
    )
    restarted = AuthManager(paths, auth.secrets)
    assert restarted.verify_session(restarted.issue_session())
    assert json.loads(paths.revoked_sessions.read_text())["revoked"] == {"live": now + 100}


def test_an_unreadable_revocation_file_is_ignored(auth: AuthManager, paths: Paths) -> None:
    paths.revoked_sessions.write_text("{not json")
    restarted = AuthManager(paths, auth.secrets)
    assert restarted.verify_session(restarted.issue_session())


def test_logout_over_http_survives_a_restart(
    app: FastAPI, client: TestClient, browser: TestClient, paths: Paths
) -> None:
    _login(browser, client)
    session = browser.cookies.get(SESSION_COOKIE)
    assert browser.post("/api/admin/auth/logout").status_code == 204
    restarted = AuthManager(paths, app.state.manager.secrets)
    assert not restarted.verify_session(session)


def test_the_manager_creates_the_api_key(paths: Paths) -> None:
    secrets = SecretStore(MemoryBackend())
    auth = AuthManager(paths, secrets)
    assert auth.ensure_api_key() is True
    key = secrets.get(SecretName.API_KEY)
    assert key and key.startswith("sk-splash-")
    assert auth.ensure_api_key() is False and secrets.get(SecretName.API_KEY) == key

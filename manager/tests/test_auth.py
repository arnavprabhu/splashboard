"""Admin auth, CSRF and the Host allowlist (SPEC §8.2, §14, §17.1)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.auth.core import SESSION_COOKIE, SESSION_TTL_S, AuthManager
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
        assert auth.login_allowed(now=float(i))
        auth.record_login_failure(now=float(i))
    assert not auth.login_allowed(now=10.0)
    assert auth.login_allowed(now=200.0)


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


def test_reads_need_no_origin(app: FastAPI) -> None:
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
    page = TestClient(app, base_url=ORIGIN, client=LOOPBACK_CLIENT)
    response = page.post("/api/admin/settings/secrets/api-key", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"] == {
        "message": "Cross-site request refused: admin changes need a same-origin page "
        "or the CLI token",
        "type": "permission_error",
        "code": "csrf_refused",
    }
    assert not app.state.manager.secrets.has(SecretName.API_KEY)


def test_same_origin_page_may_write(browser: TestClient) -> None:
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
    client.post("/api/admin/settings/secrets/api-key")
    doc = client.get("/api/admin/settings").json()["settings"]
    doc["global"]["server"]["allowed_hosts"] = ["mymac.local"]
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


def test_admin_pages_reach_the_api_with_admin_sign_in_off(
    app: FastAPI, client: TestClient, browser: TestClient
) -> None:
    # The wizard's key toggle (or a LAN bind) turns api_key_required on with admin
    # sign-in off: the web Chat/Playground then has no cookie and was refused (401).
    _require_api_key(client)
    assert browser.get("/v1/models").status_code == 200
    # A same-origin GET carries no Origin header, only Sec-Fetch-Site.
    plain_get = TestClient(
        app, base_url=ORIGIN, client=LOOPBACK_CLIENT, headers={"Sec-Fetch-Site": "same-origin"}
    )
    assert plain_get.get("/v1/models").status_code == 200
    evil = plain_get.get("/v1/models", headers={"Origin": "http://evil.example"})
    assert evil.status_code == 403
    cross = TestClient(app, base_url=ORIGIN, client=LOOPBACK_CLIENT)
    assert cross.get("/v1/models").status_code == 401, "no same-origin headers, no pass"
    lan = TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("192.168.1.20", 50000),
        headers={"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
    )
    assert lan.get("/v1/models").status_code == 401, "forged headers from the LAN"
    # With admin sign-in on, the page needs its session cookie again.
    _require_admin_key(client)
    assert browser.get("/v1/models").status_code == 401


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

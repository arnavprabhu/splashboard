"""Round 3: owner decisions D42–D45 and the review leftovers (session3-review.md
R32–R34). Every secret here goes to the in-memory (or a fake-`security`) backend,
never the real Keychain."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.app import AppConfig, create_app
from splash_gui.integrations.service import IntegrationsService
from splash_gui.mcp.secrets import secret_name
from splash_gui.paths import Paths
from splash_gui.secrets import KeychainBackend, MemoryBackend, SecretStore
from splash_gui.system.macos import RecordingMacOS

from .conftest import LOOPBACK_CLIENT, fake_engine, write_script
from .fakeengine import MODEL, MODEL_27B, EngineHarness

H = Callable[..., EngineHarness]

# --- D43: MCP env/headers in the Keychain ------------------------------------------------


def put_servers(client: TestClient, servers: dict[str, Any]) -> Any:
    document = client.get("/api/admin/settings").json()["settings"]
    document["global"]["chat"]["mcp_servers"] = servers
    return client.put("/api/admin/settings", json=document)


TOKEN = "ghp_" + "a" * 20 + "WXYZ"
HEADER = 'Bearer tok "with" spaces\\1234'


def test_values_go_to_the_store_and_come_back_masked(
    app: FastAPI, client: TestClient, paths: Paths
) -> None:
    response = put_servers(
        client,
        {
            "gh": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": TOKEN, "MODE": "ro"}},
            "web": {"url": "http://localhost:3000/mcp", "headers": {"Authorization": HEADER}},
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()["settings"]["global"]["chat"]["mcp_servers"]
    assert saved["gh"]["env"]["GITHUB_TOKEN"] == {"secret": True, "masked": "ghp_••••WXYZ"}
    assert saved["gh"]["env"]["MODE"] == {"secret": True, "masked": "••••"}
    assert saved["web"]["headers"]["Authorization"] == {"secret": True, "masked": "Bear••••1234"}
    listed = client.get("/api/admin/settings").json()["settings"]["global"]["chat"]
    assert listed["mcp_servers"] == saved
    on_disk = paths.settings_file.read_text()
    assert TOKEN not in on_disk and "spaces" not in on_disk and '"ro"' not in on_disk
    secrets = app.state.manager.secrets
    assert secrets.get_text(secret_name("gh", "env", "GITHUB_TOKEN")) == TOKEN
    assert secrets.get_text(secret_name("web", "headers", "Authorization")) == HEADER
    assert client.get("/api/admin/mcp/servers").json()["servers"]["gh"]["env"] == saved["gh"]["env"]
    assert TOKEN not in json.dumps(client.post("/api/admin/diagnostics").json())


def test_unchanged_reference_keeps_new_string_replaces_removal_deletes(
    app: FastAPI, client: TestClient
) -> None:
    secrets = app.state.manager.secrets
    put_servers(client, {"gh": {"command": "x", "env": {"A": TOKEN, "B": "second-value-0000"}}})
    servers = client.get("/api/admin/settings").json()["settings"]["global"]["chat"]["mcp_servers"]
    servers["gh"]["env"]["A"] = servers["gh"]["env"]["A"]  # the object, unchanged
    servers["gh"]["env"]["B"] = "replaced-value-9999"
    assert put_servers(client, servers).status_code == 200
    assert secrets.get_text(secret_name("gh", "env", "A")) == TOKEN
    assert secrets.get_text(secret_name("gh", "env", "B")) == "replaced-value-9999"
    del servers["gh"]["env"]["B"]
    servers["gh"]["env"]["A"] = {"secret": True, "masked": "ghp_••••WXYZ"}
    assert put_servers(client, servers).status_code == 200
    assert secrets.get_text(secret_name("gh", "env", "B")) is None
    assert put_servers(client, {}).status_code == 200
    assert secrets.get_text(secret_name("gh", "env", "A")) is None


def test_a_reference_without_a_stored_value_is_refused(app: FastAPI, client: TestClient) -> None:
    response = put_servers(
        client, {"gh": {"command": "x", "env": {"A": {"secret": True, "masked": "x••••y"}}}}
    )
    assert response.status_code == 422
    issue = response.json()["error"]["issues"][0]
    assert issue["code"] == "secret_missing" and issue["path"][-1] == "A"


def test_a_refused_save_writes_no_secret(app: FastAPI, client: TestClient) -> None:
    document = client.get("/api/admin/settings").json()["settings"]
    document["global"]["chat"]["mcp_servers"] = {"gh": {"command": "x", "env": {"A": TOKEN}}}
    document["global"]["serve"]["queue_size"] = 0  # invalid
    assert client.put("/api/admin/settings", json=document).status_code == 422
    assert app.state.manager.secrets.get_text(secret_name("gh", "env", "A")) is None


def test_mcp_servers_route_uses_the_same_contract(app: FastAPI, client: TestClient) -> None:
    body = {"servers": {"gh": {"command": "x", "env": {"A": TOKEN}}}}
    saved = client.put("/api/admin/mcp/servers", json=body).json()["servers"]
    assert saved["gh"]["env"]["A"] == {"secret": True, "masked": "ghp_••••WXYZ"}
    again = client.put("/api/admin/mcp/servers", json={"servers": saved})
    assert again.status_code == 200
    assert app.state.manager.secrets.get_text(secret_name("gh", "env", "A")) == TOKEN


ECHO_SERVER = r"""
import json, os, sys
for raw in sys.stdin:
    msg = json.loads(raw)
    if msg.get("id") is None:
        continue
    method = msg["method"]
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {}}
    elif method == "tools/list":
        result = {"tools": [{"name": "whoami", "inputSchema": {"type": "object"}}]}
    else:
        result = {"content": [{"type": "text", "text": os.environ.get("SECRET_TOKEN", "")}]}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
"""


def test_the_server_receives_the_real_value(client: TestClient, tmp_path: Path) -> None:
    script = tmp_path / "echo.py"
    script.write_text(ECHO_SERVER)
    server = {"command": sys.executable, "args": [str(script)], "always_allow": True}
    put_servers(client, {"echo": {**server, "env": {"SECRET_TOKEN": TOKEN}}})
    call = {"server": "echo", "tool": "whoami", "arguments": {}}
    assert client.post("/api/admin/mcp/call", json=call).json()["content"][0]["text"] == TOKEN
    servers = client.get("/api/admin/mcp/servers").json()["servers"]
    servers["echo"]["env"]["SECRET_TOKEN"] = "ghp_" + "b" * 20 + "WXYZ"  # same mask
    put_servers(client, servers)
    answer = client.post("/api/admin/mcp/call", json=call).json()["content"][0]["text"]
    assert answer == "ghp_" + "b" * 20 + "WXYZ", "a new value with the same mask reconnects"


def test_plaintext_values_are_migrated_at_startup(
    paths: Paths, web_dist: Path, isolated_home: Path
) -> None:
    secrets = SecretStore(MemoryBackend())
    paths.settings_file.write_text(
        json.dumps(
            {
                "version": 1,
                "global": {
                    "chat": {
                        "mcp_servers": {
                            "gh": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": TOKEN}},
                            "web": {"url": "http://x/mcp", "headers": {"Authorization": HEADER}},
                        }
                    }
                },
                "models": {},
            }
        )
    )
    app = create_app(AppConfig(paths=paths, web_dist=web_dist, secrets=secrets))
    app.state.manager.discover_engine = fake_engine
    with TestClient(app, client=LOOPBACK_CLIENT):
        pass
    on_disk = paths.settings_file.read_text()
    assert TOKEN not in on_disk and "spaces" not in on_disk
    stored = json.loads(on_disk)["global"]["chat"]["mcp_servers"]
    assert stored["gh"]["env"]["GITHUB_TOKEN"] == {"secret": True, "masked": "ghp_••••WXYZ"}
    assert secrets.get_text(secret_name("gh", "env", "GITHUB_TOKEN")) == TOKEN
    assert secrets.get_text(secret_name("web", "headers", "Authorization")) == HEADER


def test_keychain_backend_names_and_encoding(tmp_path: Path) -> None:
    """Against a fake `security`: the MCP item name is fixed-alphabet and the value
    reaches `security -i` base64-encoded, so quotes and spaces never break it."""
    log = tmp_path / "security.log"
    fake = write_script(tmp_path / "security", f'echo "$@" >> "{log}"\ncat >> "{log}"\n')
    store = SecretStore(KeychainBackend(security=str(fake)))
    store.set_text(secret_name("web", "headers", "Authorization"), HEADER)
    written = log.read_text()
    assert "ai.splashgui.mcp." in written and "b64:" in written
    assert "spaces" not in written and '"with"' not in written


# --- D42: a LAN bind needs admin sign-in (validation tested in test_settings_validation) --


def test_lan_bind_with_admin_sign_in_saves(client: TestClient) -> None:
    client.post("/api/admin/settings/secrets/api-key")
    document = client.get("/api/admin/settings").json()["settings"]
    document["global"]["server"]["host"] = "0.0.0.0"  # noqa: S104
    document["global"]["security"]["api_key_required"] = True
    document["global"]["security"]["admin_requires_key"] = False
    refused = client.put("/api/admin/settings", json=document)
    assert refused.json()["error"]["issues"][0]["code"] == "lan_requires_admin_key"
    assert client.get("/api/admin/settings").json()["settings"]["global"]["server"]["host"] == (
        "127.0.0.1"
    ), "nothing was saved and nothing switched on silently"


# --- D44: warnings -----------------------------------------------------------------------------


def test_no_row_warns_about_a_plaintext_key_from_splash_1_3(client: TestClient) -> None:
    """Splash 1.3.0 keeps the key out of the Hermes profile, so the D44 warning and
    its API field are gone (D54); the notes say what the profile stores."""
    client.post("/api/admin/settings/secrets/api-key")
    document = client.get("/api/admin/settings").json()["settings"]
    document["global"]["security"]["api_key_required"] = True
    assert client.put("/api/admin/settings", json=document).status_code == 200
    rows = {r["name"]: r for r in client.post("/api/admin/integrations").json()["cli"]}
    assert not any("plaintext_key_warning" in r for r in rows.values())
    assert rows["hermes"]["changes"]["notes"][1:] == [
        "The profile's api_key is `${SPLASH_API_KEY}`; the key itself is never saved.",
        "A profile written by Splash 1.2.x keeps the old key until the next "
        "`splash launch hermes` rewrites it.",
    ]


def test_storage_flags_a_models_dir_shared_with_the_hf_cache(
    client: TestClient, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = client.get("/api/admin/storage").json()
    assert info["models_shared_with_hf_cache"] is False
    assert info["hf_cache_path"] == str(isolated_home / "hf-home" / "hub")
    monkeypatch.setenv("HF_HUB_CACHE", info["models_dir"])
    shared = client.get("/api/admin/storage").json()
    assert shared["models_shared_with_hf_cache"] is True
    assert shared["hf_cache_path"] == info["models_dir"]


# --- D45: startup crashes of an auto-restart count toward the crash loop ---------------


def test_auto_restart_startup_crashes_reach_the_crash_loop(
    harness_factory: H, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness_factory()
    published: list[tuple[str, Any]] = []
    real = h.state.events.publish

    def spy(event: str, data: Any) -> None:
        published.append((event, data))
        real(event, data)

    h.state.events.publish = spy
    h.load()
    # Every engine started from now on dies during startup.
    monkeypatch.setenv("FAKE_SPLASH_FAIL_STARTUP", "crash")
    h.fake("POST", "/_fake/crash", {"signal": "SIGKILL"})
    view = h.wait_state("failed", timeout=20)
    assert view["error"]["kind"] == "crash_loop", view["error"]
    assert view["restart"]["crashes_in_window"] == 3
    assert any(getattr(d, "condition", None) == "crash_loop" for e, d in published if e == "alert")
    assert any(
        getattr(d, "kind", None) == "crash_loop" for e, d in published if e == "notification"
    )


def test_a_first_start_that_crashes_is_still_failed_not_a_loop(harness_factory: H) -> None:
    h = harness_factory(env={"FAKE_SPLASH_FAIL_STARTUP": "crash"})
    view = h.load()
    assert view["state"] == "failed" and view["error"]["kind"] != "crash_loop"


# --- Review leftovers ------------------------------------------------------------------------


def test_a_request_never_reaches_a_model_switched_underneath(harness_factory: H) -> None:
    """R32: the route said MODEL_27B but another request left MODEL loaded."""
    from splash_gui.proxy.pipeline import Route

    h = harness_factory(installed=(MODEL, MODEL_27B))
    h.load(MODEL)
    pipeline = h.state.proxy

    async def stale_route(request: Any, requested: Any) -> Route:
        return Route(model=MODEL_27B, request_model=MODEL_27B, profile=None, overlay={})

    pipeline.route = stale_route
    body = {"model": MODEL_27B, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4}
    response = h.client.post("/v1/chat/completions", json=body)
    assert response.status_code == 503 and response.headers["retry-after"] == "10"
    assert response.json()["error"]["code"] == "model_switch_busy"
    sent = [r for r in h.last_engine_requests() if r["path"] == "/v1/chat/completions"]
    assert sent == [], "nothing was forwarded to the wrong model"


def test_print_and_changes_use_the_bound_port(
    app: FastAPI, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = app.state.manager
    service = IntegrationsService(state, home=tmp_path / "home")
    monkeypatch.setattr(state, "integrations", service)
    monkeypatch.setattr(state, "bound", ("127.0.0.1", 9123))
    claude = client.post("/api/admin/integrations/claude/print", params={"model": MODEL}).json()
    assert claude["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9123"
    hermes = client.post("/api/admin/integrations/hermes/print", params={"model": MODEL}).json()
    assert hermes["files"][0]["path"].endswith("/profiles/splash-9123/config.yaml")


@pytest.fixture
def svc(app: FastAPI, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    home = tmp_path / "home"
    (home / "Applications" / "Codex.app").mkdir(parents=True)
    (home / "Applications" / "Claude.app").mkdir(parents=True)
    state = app.state.manager
    monkeypatch.setattr(state, "macos", RecordingMacOS())
    service = IntegrationsService(state, home=home)
    monkeypatch.setattr(state, "integrations", service)
    monkeypatch.setattr(service, "native_codex_models", lambda codex_home: [])
    monkeypatch.setattr(service, "env", lambda: {})
    return service


async def test_a_lost_record_with_our_config_refuses_connect(svc: Any) -> None:
    config = svc.home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('openai_base_url = "http://127.0.0.1:8000/api/codex/v1"\n')
    from splash_gui.errors import ApiError

    with pytest.raises(ApiError) as caught:
        await svc.connect("codex-app", confirm=False)
    assert caught.value.code == "foreign_connection_state"
    assert config.read_text() == 'openai_base_url = "http://127.0.0.1:8000/api/codex/v1"\n'


async def test_a_corrupt_state_file_is_kept_and_reported(svc: Any) -> None:
    svc.state.paths.integrations_state.write_text("{not json")
    await svc.start()
    listing = svc.listing()
    assert listing.corrupt_state and Path(listing.corrupt_state).read_text() == "{not json"


async def test_a_symlinked_config_is_edited_at_its_target(svc: Any) -> None:
    real = svc.home / "dotfiles" / "codex.toml"
    real.parent.mkdir(parents=True)
    original = b'model = "gpt-5"\n'
    real.write_bytes(original)
    link = svc.home / ".codex" / "config.toml"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    await svc.connect("codex-app", confirm=False)
    assert link.is_symlink() and b"/api/codex/t/" in real.read_bytes()
    await svc.disconnect("codex-app")
    assert link.is_symlink() and real.read_bytes() == original


async def test_a_broken_symlink_is_refused(svc: Any) -> None:
    from splash_gui.errors import ApiError

    link = svc.home / ".codex" / "config.toml"
    link.parent.mkdir(parents=True)
    link.symlink_to(svc.home / "missing" / "dir" / "config.toml")
    with pytest.raises(ApiError) as caught:
        await svc.connect("codex-app", confirm=False)
    assert caught.value.code == "broken_symlink"
    assert not (svc.home / "missing").exists(), "no parents created for the target"
    assert "codex-app" not in svc.records


def test_hermes_and_pi_follow_their_environment(
    svc: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_root = tmp_path / "elsewhere" / "hermes"
    (hermes_root / "profiles" / "splash").mkdir(parents=True)
    (hermes_root / "profiles" / "splash" / "config.yaml").write_text("x: 1\n")
    pi_dir = tmp_path / "pi-agent"
    pi_dir.mkdir()
    (pi_dir / "models.json").write_text(json.dumps({"providers": {"splash-9000": {}, "o": {}}}))
    env = {
        "HERMES_HOME": str(hermes_root / "profiles" / "work"),
        "PI_CODING_AGENT_DIR": str(pi_dir),
    }
    monkeypatch.setattr(svc, "env", lambda: env)
    assert svc.entries("hermes") == ["splash"]
    assert svc.entries("pi") == ["splash-9000"]
    assert svc.remove_entries("hermes").removed == ["splash"]
    assert not (hermes_root / "profiles" / "splash").exists()
    svc.remove_entries("pi")
    assert json.loads((pi_dir / "models.json").read_text())["providers"] == {"o": {}}
    rows = {r.name: r for r in svc.listing().cli}
    assert rows["hermes"].changes.files[0].startswith(str(hermes_root / "profiles"))
    assert rows["pi"].changes.files == [str(pi_dir / "models.json")]


@pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")
def test_splash_rm_asks_again_for_the_loaded_model(
    harness_factory: H, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from splash_gui import cli as cli_module

    from .test_cli_commands import _Shared

    h = harness_factory()
    real = cli_module.Client

    class Client(real):  # type: ignore[misc, valid-type]
        def __init__(self, port: int | None = None) -> None:
            super().__init__(port)
            self.http.close()
            self.http: Any = _Shared(h.client)

    monkeypatch.setattr(cli_module, "Client", Client)
    h.load()
    answers = iter(["y", "n"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert cli_module.main(["rm", MODEL]) == 1
    assert "active" in capsys.readouterr().err.lower()
    assert h.engine()["state"] in ("ready", "busy"), "the loaded model was not deleted"
    assert cli_module.main(["rm", MODEL, "--yes"]) == 0
    assert h.client.get("/api/admin/models").json()["models"] == []

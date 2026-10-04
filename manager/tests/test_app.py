"""App factory, SPA serving and the admin API (SPEC §7.1, §14)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from splash_gui.app import AppConfig, create_app
from splash_gui.paths import Paths
from splash_gui.secrets import SecretName, SecretStore

MLX = "mlx-community/Qwen3.8-27B-4bit"
GGUF = "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M"

# Every route in SPEC §14 (path parameters as FastAPI names them).
SPEC_ROUTES = [
    ("get", "/system"),
    ("get", "/versions"),
    ("post", "/engine/upgrade"),
    ("get", "/doctor"),
    ("get", "/engine"),
    ("post", "/engine/load"),
    ("post", "/engine/stop"),
    ("post", "/engine/restart"),
    ("get", "/engine/status"),
    ("post", "/engine/raw/{path}"),
    ("get", "/events"),
    ("get", "/metrics/live"),
    ("get", "/metrics/series"),
    ("get", "/settings"),
    ("put", "/settings"),
    ("post", "/settings/validate"),
    ("get", "/settings/schema"),
    ("get", "/models"),
    ("get", "/models/{model_id}"),
    ("delete", "/models/{model_id}"),
    ("post", "/models/{model_id}/verify"),
    ("post", "/models/{model_id}/update"),
    ("get", "/catalog"),
    ("get", "/search"),
    ("get", "/inspect"),
    ("get", "/card"),
    ("get", "/downloads"),
    ("post", "/downloads"),
    ("post", "/downloads/{dl}/pause"),
    ("post", "/downloads/{dl}/resume"),
    ("delete", "/downloads/{dl}"),
    ("get", "/models/{model_id}/profiles"),
    ("put", "/models/{model_id}/profiles"),
    ("get", "/chats"),
    ("post", "/chats"),
    ("get", "/chats/{cid}"),
    ("put", "/chats/{cid}"),
    ("delete", "/chats/{cid}"),
    ("get", "/chats/{cid}/export"),
    ("delete", "/chats"),
    ("get", "/mcp/servers"),
    ("put", "/mcp/servers"),
    ("get", "/mcp/tools"),
    ("post", "/mcp/call"),
    ("get", "/usage/summary"),
    ("get", "/usage/timeseries"),
    ("get", "/usage/requests"),
    ("get", "/usage/export.csv"),
    ("delete", "/usage"),
    ("post", "/benchmark"),
    ("get", "/benchmark/runs"),
    ("get", "/benchmark/runs/{rid}"),
    ("delete", "/benchmark/runs/{rid}"),
    ("post", "/benchmark/cancel"),
    ("get", "/integrations"),
    ("post", "/integrations/{name}/connect"),
    ("post", "/integrations/{name}/disconnect"),
    ("post", "/integrations/restore-all"),
    ("post", "/integrations/{name}/open-terminal"),
    ("delete", "/integrations/{name}/entries"),
    ("get", "/logs/{source}"),
    ("get", "/logs/{source}/stream"),
    ("get", "/logs/{source}/download"),
    ("delete", "/logs"),
    ("get", "/traces"),
    ("post", "/traces/{name}/replay"),
    ("delete", "/traces/{name}"),
    ("get", "/data/sizes"),
    ("post", "/data/clear"),
    ("post", "/auth/login"),
    ("post", "/auth/logout"),
    ("get", "/auth/state"),
]


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200 and response.json()["status"] == "ok"


def test_root_redirects_to_admin(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307) and response.headers["location"] == "/admin/"


@pytest.mark.parametrize(
    "path",
    [
        "/admin",
        "/admin/",
        "/admin/models/downloader",
        "/admin/models/mlx-community/Qwen3.8-27B-4bit/settings",
    ],
)
def test_spa_fallback(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert "<title>SPA</title>" in response.text
    assert response.headers["cache-control"] == "no-cache"


def test_hashed_assets_are_immutable(client: TestClient) -> None:
    response = client.get("/admin/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/admin/favicon.svg").headers["cache-control"] == "no-cache"


def test_missing_files_are_404(client: TestClient) -> None:
    assert client.get("/admin/assets/nope.js").status_code == 404
    response = client.get("/admin/robots.txt")
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"


def test_no_path_traversal(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("nope")
    response = client.get("/admin/..%2Fsecret.txt")
    assert "nope" not in response.text


def test_unbuilt_spa(paths: Paths, secrets: SecretStore, tmp_path: Path) -> None:
    app = create_app(AppConfig(paths=paths, web_dist=tmp_path / "missing", secrets=secrets))
    response = TestClient(app).get("/admin/")
    assert response.status_code == 503 and "not built" in response.text


def test_web_dist_env(
    paths: Paths, secrets: SecretStore, web_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPLASH_GUI_WEB_DIST", str(web_dist))
    app = create_app(AppConfig(paths=paths, secrets=secrets))
    assert TestClient(app).get("/admin/").status_code == 200


def test_openapi_covers_every_spec_route(client: TestClient) -> None:
    spec = client.get("/api/admin/openapi.json").json()
    missing = [
        (method, path)
        for method, path in SPEC_ROUTES
        if method not in spec["paths"].get(f"/api/admin{path}", {})
    ]
    assert missing == []
    assert "SettingsDocument" in spec["components"]["schemas"]
    assert "EngineView" in spec["components"]["schemas"]
    for sse_only in (
        "LiveMetrics",
        "HelloEvent",
        "Alert",
        "Notification",
        "JobEvent",
        "SettingsChangedEvent",
        "LogLine",
    ):
        assert sse_only in spec["components"]["schemas"], sse_only


def test_no_route_is_left_as_a_stub() -> None:
    """Every SPEC §14 route is implemented: nothing calls `not_implemented` any more."""
    import splash_gui

    root = Path(splash_gui.__file__).parent
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path.name != "errors.py" and "not_implemented(" in path.read_text()
    ]
    assert offenders == []


def test_unknown_admin_route_is_json_404(client: TestClient) -> None:
    response = client.get("/api/admin/nope")
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"


def test_engine_view_when_stopped(client: TestClient) -> None:
    body = client.get("/api/admin/engine").json()
    assert body["state"] == "stopped" and body["engine"]["version"] == "1.2.0"
    assert body["restart"]["auto_restart"] is True
    status = client.get("/api/admin/engine/status")
    assert status.status_code == 503 and status.headers["retry-after"] == "5"
    assert status.json()["error"]["code"] == "engine_unavailable"


def test_versions(client: TestClient) -> None:
    body = client.get("/api/admin/versions").json()
    assert body["engine"]["support"] == "supported"
    assert body["engine"]["supported_range"] == ">=1.2.0 <1.3.0"


def test_system(client: TestClient) -> None:
    body = client.get("/api/admin/system").json()
    assert body["memory_bytes"] >= 0 and "power" in body and "models" in body["disk"]


# Settings --------------------------------------------------------------------------


def test_get_settings(client: TestClient, paths: Paths) -> None:
    body = client.get("/api/admin/settings").json()
    assert body["settings"]["version"] == 1
    assert body["settings"]["global"]["server"]["port"] == 8000
    assert body["secrets"]["api_key_set"] is False
    assert body["resolved"]["models_dir"] == str(paths.models_dir)
    assert body["resolved"]["tmp_dir"] == str(paths.cache_dir / "tmp")


def _settings(client: TestClient) -> dict[str, Any]:
    data: dict[str, Any] = client.get("/api/admin/settings").json()["settings"]
    return data


def test_put_settings_valid(client: TestClient, paths: Paths) -> None:
    doc = _settings(client)
    doc["global"]["serve"]["max_context"] = "64K"
    doc["global"]["routing"]["auto_load"] = False
    response = client.put("/api/admin/settings", json=doc)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restart_required"] is False  # no engine running
    changed = {(c["key"], c["applies"]) for c in body["changed"]}
    assert changed == {("serve.max_context", "next_load"), ("routing.auto_load", "immediate")}
    assert paths.settings_file.exists()


def test_put_settings_restart_required_for_active_model(client: TestClient) -> None:
    client.app.state.manager.active_model = lambda: MLX  # type: ignore[attr-defined]
    doc = _settings(client)
    doc["global"]["serve"]["queue_size"] = 8
    body = client.put("/api/admin/settings", json=doc).json()
    assert body["restart_required"] is True
    assert body["changed"] == [{"key": "serve.queue_size", "model": None, "applies": "restart"}]
    # A change for another model applies on its next load.
    doc["models"][GGUF] = {"serve": {"language_only": True}}
    body = client.put("/api/admin/settings", json=doc).json()
    assert body["restart_required"] is False
    assert body["changed"] == [
        {"key": "serve.language_only", "model": GGUF, "applies": "next_load"}
    ]


def test_global_change_masked_by_active_model_override(client: TestClient) -> None:
    client.app.state.manager.active_model = lambda: MLX  # type: ignore[attr-defined]
    doc = _settings(client)
    doc["models"][MLX] = {"serve": {"max_context": "32K"}}
    client.put("/api/admin/settings", json=doc)
    doc["global"]["serve"]["max_context"] = "64K"
    body = client.put("/api/admin/settings", json=doc).json()
    assert body["restart_required"] is False
    assert body["changed"] == [{"key": "serve.max_context", "model": None, "applies": "next_load"}]


def test_settings_listeners(client: TestClient) -> None:
    seen: list[Any] = []
    client.app.state.manager.settings_listeners.append(  # type: ignore[attr-defined]
        lambda changes, restart: seen.append(([c.key for c in changes], restart))
    )
    doc = _settings(client)
    doc["global"]["ui"]["theme"] = "dark"
    client.put("/api/admin/settings", json=doc)
    assert seen == [(["ui.theme"], False)]


def test_put_settings_invalid(client: TestClient, paths: Paths) -> None:
    doc = _settings(client)
    doc["global"]["serve"]["persistent_cache"] = True
    doc["global"]["serve"]["max_context"] = "999K"
    response = client.put("/api/admin/settings", json=doc)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_settings"
    assert error["issues"][0]["path"] == ["global", "serve", "max_context"]
    assert error["issues"][0]["key"] == "serve.max_context"
    assert not paths.settings_file.exists()


def test_validate_route(client: TestClient) -> None:
    doc = _settings(client)
    doc["global"]["serve"]["persistent_cache"] = True
    body = client.post("/api/admin/settings/validate", json=doc).json()
    assert body["valid"] is False
    assert body["errors"][0]["message"] == "--persistent-cache needs --max-cache-disk"
    doc["global"]["serve"]["max_cache_disk"] = "100M"
    body = client.post("/api/admin/settings/validate", json=doc).json()
    assert body["valid"] is True and body["warnings"][0]["key"] == "serve.max_cache_disk"


def test_lan_bind_needs_generated_key(client: TestClient) -> None:
    doc = _settings(client)
    doc["global"]["server"]["host"] = "0.0.0.0"  # noqa: S104
    doc["global"]["security"]["api_key_required"] = True
    assert client.put("/api/admin/settings", json=doc).status_code == 422
    key = client.post("/api/admin/settings/secrets/api-key").json()["key"]
    assert key.startswith("sk-splash-")
    refused = client.put("/api/admin/settings", json=doc)
    assert refused.status_code == 422, "D42: admin sign-in must be on for a LAN bind"
    issue = refused.json()["error"]["issues"][0]
    assert (
        issue["key"] == "security.admin_requires_key" and issue["code"] == "lan_requires_admin_key"
    )
    doc["global"]["security"]["admin_requires_key"] = True
    assert client.put("/api/admin/settings", json=doc).status_code == 200
    # The key cannot be deleted while it is required.
    assert client.delete("/api/admin/settings/secrets/api-key").status_code == 409


def test_schema(client: TestClient) -> None:
    body = client.get("/api/admin/settings/schema").json()
    fields = {f["key"]: f for f in body["fields"]}
    mc = fields["serve.max_context"]
    assert mc["flag"] == "--max-context" and mc["applies"] == "restart"
    assert mc["scope"] == "GM" and mc["section"] == "memory_context" and mc["default"] == "auto"
    assert fields["serve.revision"]["disabled_for_legacy"] is True
    assert fields["security.api_key"]["storage"] == "keychain"
    assert [s["label"] for s in body["sections"]][:3] == [
        "Server & network",
        "Security",
        "Models & storage",
    ]
    assert {f["key"] for f in body["profile_fields"]} >= {"temperature", "reasoning_effort"}
    assert body["engine_options"]["available"] is False  # fake engine has no Python


def test_effective_and_launch_preview(client: TestClient) -> None:
    doc = _settings(client)
    doc["global"]["serve"]["max_context"] = "64K"
    doc["models"][MLX] = {"serve": {"max_context": "128K"}}
    client.put("/api/admin/settings", json=doc)
    eff = client.get("/api/admin/settings/effective", params={"model": MLX}).json()
    assert eff["values"]["serve.max_context"] == {
        "key": "serve.max_context",
        "value": "128K",
        "source": "model",
        "default": "auto",
        "global_value": "64K",
        "model_value": "128K",
    }
    assert {p["id"] for p in eff["profiles"]} >= {f"{MLX}:no-think", f"{MLX}:default"}
    preview = client.get("/api/admin/settings/launch-preview", params={"model": MLX}).json()
    assert preview["argv"][:4] == ["/opt/homebrew/opt/splash/bin/splash", "serve", "--model", MLX]
    assert "--max-context" in preview["argv"] and preview["env"]["SPLASH_API_KEY"] != ""
    assert "splash-internal-preview" not in preview["display"]
    assert (
        client.get("/api/admin/settings/launch-preview", params={"model": "bad"}).status_code == 400
    )


def test_presets(client: TestClient) -> None:
    client.app.state.manager.memory_bytes = lambda: 64 * 1024**3  # type: ignore[attr-defined]
    body = client.get("/api/admin/settings/presets").json()
    coding = next(p for p in body["presets"] if p["id"] == "coding")
    assert coding["recommendation"]["primary"]["model"] == MLX
    applied = client.post("/api/admin/settings/presets/coding/apply", json={}).json()
    assert applied["settings"]["global"]["serve"]["persistent_cache"] is True
    assert applied["settings"]["global"]["wizard"]["preset"] == "coding"


# Profiles ---------------------------------------------------------------------------


def test_profiles_route_is_not_swallowed_by_models(client: TestClient) -> None:
    response = client.get(f"/api/admin/models/{GGUF}/profiles")
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == GGUF
    assert [p["name"] for p in body["profiles"]] == [
        "default",
        "no-think",
        "deterministic",
        "qwen-nonthinking",
    ]
    # The bare model route still reaches the models router.
    assert client.get(f"/api/admin/models/{GGUF}").status_code == 404


def test_put_profiles(client: TestClient) -> None:
    response = client.put(
        f"/api/admin/models/{MLX}/profiles",
        json={
            "profiles": {"fast": {"max_tokens": 256, "temperature": 0.2}, "no-think": None},
            "sampling_defaults": {"temperature": 0.6},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    names = [p["name"] for p in body["profiles"]]
    assert "no-think" not in names and "fast" in names
    assert body["sampling_defaults"] == {"temperature": 0.6}
    fast = next(p for p in body["profiles"] if p["name"] == "fast")
    assert fast["id"] == f"{MLX}:fast" and fast["builtin"] is False


def test_put_profiles_validates_ranges(client: TestClient) -> None:
    response = client.put(
        f"/api/admin/models/{MLX}/profiles", json={"profiles": {"hot": {"temperature": 3}}}
    )
    assert response.status_code == 422
    assert "temperature must be a number in [0, 2]" in response.text
    response = client.put(f"/api/admin/models/{MLX}/profiles", json={"profiles": {"Bad Name": {}}})
    assert response.status_code == 422


# Secrets ------------------------------------------------------------------------------


def test_api_key_lifecycle(client: TestClient, secrets: SecretStore) -> None:
    assert client.get("/api/admin/settings/secrets/api-key").json() == {"key": None}
    first = client.post("/api/admin/settings/secrets/api-key").json()["key"]
    second = client.post("/api/admin/settings/secrets/api-key").json()["key"]
    assert first != second and secrets.get(SecretName.API_KEY) == second
    assert client.get("/api/admin/settings").json()["secrets"]["api_key_set"] is True
    assert client.delete("/api/admin/settings/secrets/api-key").status_code == 204
    assert secrets.get(SecretName.API_KEY) is None


def test_hf_token_set_and_test(client: TestClient, secrets: SecretStore) -> None:
    response = client.put("/api/admin/settings/secrets/hf-token", json={"token": "hf_abc123"})
    assert response.json()["hf_token_override_set"] is True
    assert secrets.get(SecretName.HF_TOKEN) == "hf_abc123"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/whoami-v2"
        if request.headers["authorization"] == "Bearer hf_abc123":
            return httpx.Response(200, json={"name": "arnav", "orgs": [{"name": "incoai"}]})
        return httpx.Response(401, json={"error": "bad"})

    client.app.state.http_transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    ok = client.post("/api/admin/settings/secrets/hf-token/test", json={}).json()
    assert ok == {
        "ok": True,
        "source": "override",
        "user": "arnav",
        "orgs": ["incoai"],
        "error": None,
    }
    bad = client.post(
        "/api/admin/settings/secrets/hf-token/test", json={"token": "hf_wrong"}
    ).json()
    assert bad["ok"] is False and bad["source"] == "provided" and "401" in bad["error"]
    assert client.delete("/api/admin/settings/secrets/hf-token").status_code == 204


# Other real routes ----------------------------------------------------------------------


def test_mcp_servers_roundtrip(client: TestClient) -> None:
    servers = {
        "files": {"command": "npx", "args": ["-y", "server-files"]},
        "web": {"url": "http://localhost:9000/mcp"},
    }
    response = client.put("/api/admin/mcp/servers", json={"servers": servers})
    assert response.status_code == 200, response.text
    body = client.get("/api/admin/mcp/servers").json()
    assert body["servers"]["files"]["command"] == "npx"
    assert body["servers"]["web"]["always_allow"] is False
    bad = client.put(
        "/api/admin/mcp/servers", json={"servers": {"x": {"command": "a", "url": "http://b"}}}
    )
    assert bad.status_code == 422


def test_logs_tail_and_clear(client: TestClient, paths: Paths) -> None:
    paths.engine_log.write_text(
        "2026-10-03 10:00:00,000 stdout Weights loaded in 1.2 s.\n"
        "2026-10-03 10:00:01,000 stderr error: model does not fit\n"
    )
    body = client.get("/api/admin/logs/engine", params={"tail": 10}).json()
    assert [(line["level"], line["stream"]) for line in body["lines"]] == [
        ("info", "stdout"),
        ("error", "stderr"),
    ]
    assert body["lines"][0]["text"] == "Weights loaded in 1.2 s."
    download = client.get("/api/admin/logs/engine/download")
    assert download.headers["content-type"] == "application/zip"
    cleared = client.delete("/api/admin/logs").json()
    assert cleared["deleted_bytes"] > 0 and paths.engine_log.read_text() == ""
    assert client.get("/api/admin/logs/bogus").status_code == 422


def test_auth_state_open_by_default(browser: TestClient) -> None:
    assert browser.get("/api/admin/auth/state").json() == {
        "admin_requires_key": False,
        "authenticated": True,
        "method": "open",
    }


def test_storage_info(client: TestClient, paths: Paths) -> None:
    body = client.get("/api/admin/storage").json()
    assert body["models_dir"] == str(paths.models_dir)
    assert body["splash_data_dir"] == str(paths.base / "fake-data")


def test_request_validation_error_shape(client: TestClient) -> None:
    response = client.post("/api/admin/engine/load", json={})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request" and error["issues"][0]["path"] == ["model"]


def test_doctor_covers_the_spec_checks(client: TestClient) -> None:
    report = client.get("/api/admin/doctor").json()
    ids = {c["id"] for c in report["checks"]}
    assert ids >= {
        "hardware",
        "engine",
        "brew",
        "disk",
        "permissions",
        "path",
        "shim",
        "ports",
        "hf_token",
        "integrations",
    }
    by = {c["id"]: c for c in report["checks"]}
    assert by["engine"]["status"] == "ok" and "1.2.0" in by["engine"]["message"]
    assert by["permissions"]["status"] == "ok"
    assert by["hf_token"]["status"] == "warn", "the suite never sees the developer's token"
    assert by["integrations"]["status"] == "ok"


def test_install_homebrew_opens_terminal_with_the_official_command(
    app: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from splash_gui.schemas import BrewInfo
    from splash_gui.system import api as system_api
    from splash_gui.system.macos import RecordingMacOS

    recorder = RecordingMacOS()
    app.state.manager.macos = recorder
    monkeypatch.setattr(system_api, "get_brew", lambda state: BrewInfo(installed=False))
    result = client.post("/api/admin/system/brew/install").json()
    assert result["ok"] is True and "Homebrew/install/HEAD/install.sh" in result["command"]
    script = recorder.calls[-1][-1]
    assert recorder.calls[-1][0].endswith("osascript") and "install.sh" in script
    monkeypatch.setattr(system_api, "get_brew", lambda state: BrewInfo(installed=True))
    again = client.post("/api/admin/system/brew/install")
    assert again.status_code == 409 and again.json()["error"]["code"] == "brew_installed"

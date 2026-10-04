"""Routes the web admin already calls (docs/progress/session3-frontend.md,
"Backend dependencies"): Open app, connect/restore steps, View backup, launch
`--print` data, settings reset, secret meta, HF whoami, download plans, model
fingerprints, replay Stop and tokenizer pieces."""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.engine.discovery import EngineInfo
from splash_gui.integrations.service import IntegrationsService
from splash_gui.secrets import SecretName
from splash_gui.system.macos import RecordingMacOS

from .conftest import HAVE_SPLASH, REPO, SPLASH_PYTHON
from .fakeengine import MODEL, EngineHarness

H = Callable[..., EngineHarness]


# --- Integrations ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    for name in ("Claude.app", "Codex.app"):
        (home / "Applications" / name / "Contents").mkdir(parents=True)
    return home


@pytest.fixture
def svc(app: FastAPI, client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    state = app.state.manager
    recorder = RecordingMacOS()
    monkeypatch.setattr(state, "macos", recorder)
    service = IntegrationsService(state, home=home)
    monkeypatch.setattr(state, "integrations", service)
    monkeypatch.setattr(service, "native_codex_models", lambda codex_home: [])

    async def no_gateway() -> None:
        return None

    monkeypatch.setattr(service, "start_gateway", no_gateway)
    return service


def events(app: FastAPI) -> list[tuple[str, Any]]:
    seen: list[tuple[str, Any]] = []
    bus = app.state.manager.events
    real = bus.publish

    def spy(event: str, data: Any) -> None:
        seen.append((event, data))
        real(event, data)

    bus.publish = spy
    return seen


def test_open_app(
    svc: Any, client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = client.post("/api/admin/integrations/claude-desktop/open").json()
    assert claude == {"ok": True, "path": str(home / "Applications" / "Claude.app")}
    assert svc.state.macos.calls[-1] == ["/usr/bin/open", "-a", claude["path"]]
    codex = client.post("/api/admin/integrations/codex-app/open").json()
    assert svc.state.macos.calls[-1][-1] == "codex://threads/new?mode=codex"
    assert codex["path"].endswith("Codex.app")
    monkeypatch.setattr(svc, "app", lambda name: None)
    missing = client.post("/api/admin/integrations/claude-desktop/open")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "app_not_found"
    assert client.post("/api/admin/integrations/claude/open").status_code == 422


def test_open_app_failure_is_503(
    svc: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from splash_gui.system.macos import CommandResult

    monkeypatch.setattr(svc.state.macos, "open", lambda *a: CommandResult(1, "", "no app"))
    response = client.post("/api/admin/integrations/claude-desktop/open")
    assert response.status_code == 503 and response.json()["error"]["code"] == "app_open_failed"


def steps(seen: list[tuple[str, Any]]) -> list[tuple[str, str | None]]:
    return [(d.state, d.step) for e, d in seen if e == "integration.state"]


def test_connect_and_disconnect_report_their_steps(
    svc: Any, app: FastAPI, client: TestClient
) -> None:
    seen = events(app)
    assert client.post("/api/admin/integrations/claude-desktop/connect", json={}).status_code == 200
    assert steps(seen) == [
        ("connecting", "quitting_app"),
        ("connecting", "backing_up"),
        ("connecting", "starting_gateway"),
        ("connecting", "writing_config"),
        ("connecting", "opening_app"),
        ("connected", "done"),
    ]
    rows = [d for e, d in seen if e == "integration.state"]
    assert all(r.name == "claude-desktop" for r in rows) and rows[1].message
    assert client.get("/api/admin/integrations").json()["desktop"][0]["step"] is None
    seen.clear()
    assert client.post("/api/admin/integrations/claude-desktop/disconnect").status_code == 200
    assert steps(seen) == [
        ("restoring", "quitting_app"),
        ("restoring", "restoring_files"),
        ("not_connected", "done"),
    ]


def test_a_failed_connect_reports_failed_with_the_reason(
    svc: Any, app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from splash_gui.system.macos import CommandResult

    monkeypatch.setattr(svc.state.macos, "open", lambda *a: CommandResult(1, "", "cannot open"))
    seen = events(app)
    response = client.post("/api/admin/integrations/codex-app/connect", json={})
    assert response.status_code == 503
    last = [d for e, d in seen if e == "integration.state"][-1]
    assert (last.state, last.step, last.message) == ("not_connected", "failed", "cannot open")
    assert "codex-app" not in svc.records


def test_reveal_backup(svc: Any, client: TestClient, home: Path) -> None:
    none = client.post("/api/admin/integrations/codex-app/reveal-backup")
    assert none.status_code == 404 and none.json()["error"]["code"] == "no_backup"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model = "gpt-5"\n')
    client.post("/api/admin/integrations/codex-app/connect", json={})
    revealed = client.post("/api/admin/integrations/codex-app/reveal-backup").json()
    folder = Path(revealed["path"])
    assert folder.parent == svc.state.paths.integrations_backups / "codex-app"
    assert any(p.read_bytes() == b'model = "gpt-5"\n' for p in folder.iterdir())
    assert svc.state.macos.calls[-1] == ["/usr/bin/open", "-R", str(folder)]
    pi = home / ".pi" / "agent" / "models.json"
    pi.parent.mkdir(parents=True)
    pi.write_text(json.dumps({"providers": {"splash": {}}}))
    svc.remove_entries("pi")
    assert client.post("/api/admin/integrations/pi/reveal-backup").status_code == 200


def real_clients_engine() -> EngineInfo:
    return EngineInfo(
        found=True,
        cli=Path("/x/splash"),
        version="1.2.0",
        version_tuple=(1, 2, 0),
        support="supported",
        pkg=REPO / "splash",
        python=Path(sys.executable),
    )


needs_clients = pytest.mark.skipif(
    not (REPO / "splash" / "install" / "clients.py").exists(),
    reason="no Splash reference clone with install/clients.py",
)


@needs_clients
def test_print_runs_splash_s_configurator(
    svc: Any, app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state.manager, "engine_cached", real_clients_engine)
    model = f"{MODEL}:no-think"
    claude = client.get("/api/admin/integrations/claude/print", params={"model": model}).json()
    assert claude["exact"] is True and claude["model"] == model
    assert claude["env"]["ANTHROPIC_MODEL"] == model
    assert claude["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8000"
    assert claude["env"]["ANTHROPIC_AUTH_TOKEN"] == "local"
    assert "ANTHROPIC_API_KEY" in claude["removed_env"]
    assert claude["args"][claude["args"].index("--model") + 1] == model
    assert claude["command"].startswith("claude ") and claude["files"] == []
    codex = client.get("/api/admin/integrations/codex/print", params={"model": model}).json()
    assert codex["exact"] is True and f'model="{model}"' in codex["args"]
    assert "-c features.apps=false" in codex["command"], "D46 shows in the preview"
    assert "features.apps=false" not in claude["command"]
    opencode = client.get("/api/admin/integrations/opencode/print", params={"model": model}).json()
    assert f"splash/{model}" in opencode["env"]["OPENCODE_CONFIG_CONTENT"]


@needs_clients
def test_print_masks_the_api_key(
    svc: Any, app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state.manager, "engine_cached", real_clients_engine)
    key = client.post("/api/admin/settings/secrets/api-key").json()["key"]
    document = client.get("/api/admin/settings").json()["settings"]
    document["global"]["security"]["api_key_required"] = True
    assert client.put("/api/admin/settings", json=document).status_code == 200
    claude = client.get("/api/admin/integrations/claude/print", params={"model": MODEL}).json()
    assert claude["env"]["ANTHROPIC_AUTH_TOKEN"] == "••••"
    assert claude["secret_env"] == ["ANTHROPIC_AUTH_TOKEN"]
    assert key not in json.dumps(claude)


def test_print_falls_back_to_a_description(svc: Any, client: TestClient, home: Path) -> None:
    """The fake engine has no install/clients.py; Hermes and Pi are never configured
    just to preview (their configurators write their profile/provider)."""
    fallback = client.get("/api/admin/integrations/claude/print", params={"model": MODEL}).json()
    assert fallback["exact"] is False and fallback["env"]["ANTHROPIC_MODEL"] == MODEL
    hermes = client.get("/api/admin/integrations/hermes/print", params={"model": MODEL}).json()
    assert hermes["exact"] is False
    assert hermes["files"][0]["path"] == str(
        home / ".hermes" / "profiles" / "splash" / "config.yaml"
    )
    assert hermes["args"] == ["--provider", "custom", "--model", MODEL]
    pi = client.get("/api/admin/integrations/pi/print").json()
    assert pi["model"] is None and pi["files"][0]["change"]
    codex = client.get("/api/admin/integrations/codex/print", params={"model": MODEL}).json()
    assert codex["exact"] is False
    assert codex["args"][-2:] == ["-c", "features.apps=false"], "D46 in the static description"
    assert any("64 characters" in n for n in codex["notes"])


# --- Settings: reset and secret meta ---------------------------------------------------


def test_reset_restores_defaults_and_keeps_data_settings(app: FastAPI, client: TestClient) -> None:
    secrets = app.state.manager.secrets
    secrets.set(SecretName.HF_TOKEN, "hf_" + "x" * 30)
    document = client.get("/api/admin/settings").json()["settings"]
    g = document["global"]
    g["serve"]["queue_size"] = 4
    g["routing"]["load_timeout"] = 60
    g["storage"]["models_dir"] = "/tmp/elsewhere-models"
    g["wizard"] = {"completed": True, "preset": "chat"}
    g["chat"]["mcp_servers"] = {"calc": {"command": "x"}}
    document["models"] = {MODEL: {"serve": {"max_context": "64K"}}}
    assert client.put("/api/admin/settings", json=document).status_code == 200
    result = client.post("/api/admin/settings/reset").json()
    after = result["settings"]
    assert after["global"]["serve"]["queue_size"] == 32
    assert after["global"]["routing"]["load_timeout"] == 120
    assert after["models"] == {}
    assert after["global"]["storage"]["models_dir"] == "/tmp/elsewhere-models"
    assert after["global"]["wizard"] == {"completed": True, "preset": "chat"}
    assert after["global"]["chat"]["mcp_servers"] == {
        "calc": {
            "command": "x",
            "args": [],
            "env": {},
            "url": None,
            "headers": {},
            "enabled": True,
            "always_allow": False,
        }
    }
    assert result["engine_restarted"] is False
    assert result["kept"] == ["global.storage", "global.wizard", "global.chat.mcp_servers"]
    assert secrets.get(SecretName.HF_TOKEN), "secrets are kept"


def test_reset_restarts_the_engine_when_the_model_s_flags_change(harness_factory: H) -> None:
    h = harness_factory()
    h.patch_settings({"models": {MODEL: {"serve": {"max_context": "64K"}}}})
    first = h.load()["pid"]
    h.sup.busy = lambda: True
    busy = h.client.post("/api/admin/settings/reset")
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "model_switch_busy"
    del h.sup.busy
    result = h.client.post("/api/admin/settings/reset", json={"force": True}).json()
    assert result["engine_restarted"] is True and result["restart_required"] is False
    view = h.wait_state("ready")
    assert view["pid"] != first and "--max-context" not in view["command"]


def test_secret_meta_masks_and_never_returns_the_value(app: FastAPI, client: TestClient) -> None:
    empty = client.get("/api/admin/settings/secret/meta").json()
    assert empty == {
        "name": "api_key",
        "set": False,
        "prefix": None,
        "last4": None,
        "masked": None,
        "updated_at": None,
    }
    key = client.post("/api/admin/settings/secrets/api-key").json()["key"]
    meta = client.get("/api/admin/settings/secret/meta", params={"name": "api_key"}).json()
    assert meta["set"] is True and meta["prefix"] == "sk-splash-" and meta["last4"] == key[-4:]
    assert meta["masked"] == f"sk-splash-••••{key[-4:]}" and key not in json.dumps(meta)
    app.state.manager.secrets.set(SecretName.HF_TOKEN, "hf_short")
    short = client.get("/api/admin/settings/secret/meta", params={"name": "hf_token"}).json()
    assert short["masked"] == "••••" and short["last4"] is None
    assert client.get("/api/admin/settings/secret/meta", params={"name": "x"}).status_code == 422


# --- Hugging Face whoami ----------------------------------------------------------------


def whoami_transport(status: int = 200, fail: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("offline", request=request)
        assert request.url.path == "/api/whoami-v2"
        user = request.headers["authorization"].removeprefix("Bearer ")
        return httpx.Response(status, json={"name": f"user-of-{user}", "orgs": [{"name": "inco"}]})

    return httpx.MockTransport(handler)


def test_whoami_reports_source_and_user(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.get("/api/admin/hf/whoami").json() == {
        "status": "no_token",
        "source": "none",
        "user": None,
        "orgs": [],
        "http_status": None,
        "message": "No Hugging Face token",
    }
    app.state.http_transport = whoami_transport()
    monkeypatch.setenv("HF_TOKEN", "envtok")
    login = client.get("/api/admin/hf/whoami").json()
    assert (
        login["status"] == "ok" and login["source"] == "env" and login["user"] == "user-of-envtok"
    )
    assert login["orgs"] == ["inco"]
    app.state.manager.secrets.set(SecretName.HF_TOKEN, "keytok")
    active = client.get("/api/admin/hf/whoami").json()
    assert active["source"] == "override" and active["user"] == "user-of-keytok"
    assert client.get("/api/admin/hf/whoami", params={"use": "login"}).json()["source"] == "env"
    app.state.http_transport = whoami_transport(status=401)
    rejected = client.get("/api/admin/hf/whoami").json()
    assert rejected["status"] == "rejected" and rejected["http_status"] == 401
    app.state.http_transport = whoami_transport(fail=True)
    assert client.get("/api/admin/hf/whoami").json()["status"] == "unreachable"


# --- Download plan, fingerprints --------------------------------------------------------


@pytest.fixture
def hub_harness(harness_factory: H, fake_hub: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("HF_ENDPOINT", fake_hub)

    def make(**kwargs: Any) -> EngineHarness:
        harness: EngineHarness = harness_factory(env={"HF_ENDPOINT": fake_hub}, **kwargs)
        harness.patch_settings({"global": {"hf": {"endpoint": fake_hub}}})
        return harness

    return make


def test_inspect_carries_download_plans(hub_harness: Any) -> None:
    h = hub_harness(installed=())
    result = h.client.get("/api/admin/inspect", params={"id": MODEL}).json()
    plan, text_only = result["download_plan"], result["language_only_plan"]
    names = [f["name"] for f in plan["files"]]
    assert any(n.endswith("UD-Q4_K_M.gguf") for n in names)
    assert any("mmproj" in n for n in names), "the vision projector is part of the plan"
    assert not any("mmproj" in f["name"] for f in text_only["files"])
    assert {f["repo_id"] for f in plan["files"]} == {
        "unsloth/Qwen3.6-35B-A3B-GGUF",
        "incoai/Qwen3.6-35B-A3B-DFlash2",
    }, "the paired draft is counted"
    assert plan["variant"] == "UD-Q4_K_M" and plan["language_only"] is False
    assert plan["total_bytes"] == sum(f["bytes"] for f in plan["files"]) > 0
    assert plan["remaining_bytes"] == plan["total_bytes"], "nothing is downloaded yet"
    assert plan["margin_bytes"] == 2 * 1024**3 and plan["fits_on_disk"] is True
    assert text_only["total_bytes"] < plan["total_bytes"]


def test_installed_files_count_as_present(
    hub_harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = hub_harness(installed=(MODEL,))
    plan = h.client.get("/api/admin/inspect", params={"id": MODEL}).json()["download_plan"]
    present = [f for f in plan["files"] if f["present"]]
    assert present, plan
    assert plan["remaining_bytes"] == sum(f["bytes"] for f in plan["files"] if not f["present"])
    import shutil

    usage = shutil.disk_usage(h.home)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: usage._replace(free=0))
    tight = h.client.get("/api/admin/inspect", params={"id": MODEL}).json()["download_plan"]
    assert tight["free_bytes"] == 0 and tight["fits_on_disk"] is False


def test_model_detail_carries_fingerprints(harness_factory: H) -> None:
    h = harness_factory()
    assert h.client.get(f"/api/admin/models/{MODEL}").json()["fingerprints"] is None
    h.load()
    deadline = time.monotonic() + 10
    while True:
        found = h.client.get(f"/api/admin/models/{MODEL}").json()["fingerprints"]
        if found and found["build_id"]:
            break
        assert time.monotonic() < deadline, found
        time.sleep(0.1)
    status = h.client.get("/api/admin/engine/status").json()["identity"]
    assert found["build_id"] == status["cache"]["build_id"]
    assert found["loaded_model_layout_sha256"] == status["cache"]["loaded_model_layout_sha256"]
    assert found["target_model_sha256"] == status["kv"]["target_model_sha256"]
    assert found["identity"] == status
    assert found["kv_format"] == status["kv"]["format"] == "int8"
    assert found["kv_quantization"] == status["kv"]["quantization"]
    assert found["max_context"] == 262144 and found["recorded_at"]


# --- Replay Stop ---------------------------------------------------------------------------


def test_replay_can_be_stopped(harness_factory: H, tmp_path: Path) -> None:
    h = harness_factory(installed=None)
    directory = tmp_path / "crash"
    directory.mkdir()
    h.state.crash_trace_dir = directory
    name = "splash-crash-g1-20261004T100000.json"
    (directory / name).write_text(
        json.dumps({"frames": list(range(100)), "seconds_per_frame": 0.1})
    )
    out: dict[str, str] = {}

    def stream() -> None:
        out["text"] = h.client.post(f"/api/admin/traces/{name}/replay").text

    worker = threading.Thread(target=stream, daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while name not in h.state.replays:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    time.sleep(0.2)
    assert h.client.post(f"/api/admin/traces/{name}/replay/cancel").json() == {"cancelled": True}
    worker.join(10)
    assert "event: exit" in out["text"] and '"code": -15' in out["text"].replace(
        '"code":-15', '"code": -15'
    )
    assert "replayed 100 frames" not in out["text"]
    assert name not in h.state.replays
    again = h.client.post(f"/api/admin/traces/{name}/replay/cancel").json()
    assert again == {"cancelled": False}
    assert h.client.post("/api/admin/traces/bad.json/replay/cancel").status_code == 400


# --- Tokenizer pieces (SPEC §22 Q6, cheap path) ---------------------------------------------

WORDLEVEL: dict[str, Any] = {
    "version": "1.0",
    "truncation": None,
    "padding": None,
    "added_tokens": [],
    "normalizer": None,
    "pre_tokenizer": {"type": "Whitespace"},
    "post_processor": None,
    "decoder": None,
    "model": {
        "type": "WordLevel",
        "vocab": {"hello": 0, "world": 1, "[UNK]": 2},
        "unk_token": "[UNK]",
    },
}


MLX = "mlx-community/Qwen3.6-35B-A3B-4bit"


def tokenizer_path(model: str) -> Path:
    from splash_gui.models.layout import read_all
    from splash_gui.paths import splash_models_dir

    selection = next(s for s in read_all(splash_models_dir()) if s.model == model)
    return selection.link / "tokenizer" / "tokenizer.json"


def test_pieces_without_a_usable_tokenizer(harness_factory: H) -> None:
    h = harness_factory()
    none = h.client.post("/api/admin/tokenizer/pieces", json={"ids": [1]})
    assert none.status_code == 409 and none.json()["error"]["code"] == "no_model"
    missing = h.client.post("/api/admin/tokenizer/pieces", json={"ids": [1], "model": "a/b"})
    assert missing.status_code == 404
    # The fake GGUF assembly carries no derived tokenizer files.
    fake = h.client.post("/api/admin/tokenizer/pieces", json={"ids": [1], "model": MODEL})
    assert fake.status_code == 503 and fake.json()["error"]["code"] == "pieces_unavailable"
    assert h.client.post(
        "/api/admin/tokenizer/pieces", json={"ids": [], "model": MODEL}
    ).json() == {"model": MODEL, "pieces": []}


@pytest.mark.skipif(not HAVE_SPLASH, reason="needs Splash's bundled Python (it ships tokenizers)")
def test_pieces_from_the_model_s_tokenizer(harness_factory: H) -> None:
    h = harness_factory(installed=(MLX,))
    path = tokenizer_path(MLX)
    fake = h.client.post("/api/admin/tokenizer/pieces", json={"ids": [1], "model": MLX})
    assert fake.status_code == 503, "the fake's placeholder tokenizer.json is unreadable"
    path.unlink()
    path.write_text(json.dumps(WORDLEVEL))
    engine = h.state.engine_cached()
    h.state.engine_cached = lambda: EngineInfo(**{**engine.__dict__, "python": SPLASH_PYTHON})
    h.load(MLX)
    response = h.client.post("/api/admin/tokenizer/pieces", json={"ids": [0, 1, 2]})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "model": MLX,
        "pieces": [
            {"id": 0, "piece": "hello", "text": "hello"},
            {"id": 1, "piece": "world", "text": "world"},
            {"id": 2, "piece": "[UNK]", "text": "[UNK]"},
        ],
    }


def test_open_in_terminal_accepts_ids_and_profiles_only(svc: Any, client: TestClient) -> None:
    """The model is typed into a Terminal command line, so only a model ID or
    `<id>:<profile>` gets that far (D22, §7.5)."""
    for model in (MODEL, f"{MODEL}:no-think", "mlx-community/Qwen3.6-35B-A3B-4bit"):
        response = client.post(
            "/api/admin/integrations/claude/open-terminal", json={"model": model}
        )
        assert response.status_code == 200, response.text
        assert response.json()["command"].endswith(f"launch claude --model {model}")
    calls = len(svc.state.macos.calls)
    for bad in ('o/r"; touch /tmp/x; "', "o/r:p q", "not-an-id", "o/r:Bad Profile"):
        response = client.post("/api/admin/integrations/claude/open-terminal", json={"model": bad})
        assert response.status_code == 400 and response.json()["error"]["code"] == (
            "invalid_model_id"
        )
    assert len(svc.state.macos.calls) == calls, "nothing reached osascript"

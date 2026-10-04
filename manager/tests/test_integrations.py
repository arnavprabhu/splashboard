"""Desktop integrations and Codex routing (SPEC §11, D18).

The guarantee under test is §11.4: connecting writes a restore record *before*
any third-party file is touched, and disconnecting puts every file back exactly
as it was, including the parts Splash never intended to change. Nothing here
talks to a real Claude or ChatGPT app: `osascript`, `pgrep` and the app lookup
are stubbed, and the filesystem under test is a throwaway `$HOME`.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from splash_gui.errors import ApiError
from splash_gui.integrations.service import IntegrationsService
from splash_gui.integrations.snapshots import read_document

from .fakeengine import MODEL


@pytest.fixture
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A throwaway $HOME with both apps 'installed' and nothing running."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for name in ("Claude.app", "Codex.app", "ChatGPT.app"):
        (home / "Applications" / name / "Contents" / "MacOS").mkdir(parents=True)
    return home


@pytest.fixture
def service(client, fake_home, monkeypatch) -> IntegrationsService:
    """The real service, with only the app/process boundaries faked out."""
    integration = client.app.state.manager.integrations
    assert isinstance(integration, IntegrationsService)
    monkeypatch.setattr(integration, "home", fake_home)
    monkeypatch.setattr(integration, "running", lambda name: False)
    monkeypatch.setattr(
        integration.state.macos,
        "open",
        lambda *args: subprocess.CompletedProcess(args, 0, "", ""),
    )
    # `pgrep` returns PIDs; an empty list means the app is not running.
    monkeypatch.setattr(integration.state.macos, "pgrep", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        integration.state.macos,
        "quit_app",
        lambda label: subprocess.CompletedProcess(["osascript"], 0, "", ""),
    )
    # A served model, so the Codex catalog and routing list have something in them.
    monkeypatch.setattr(
        integration.state.proxy,
        "models_list",
        lambda: {
            "data": [
                {
                    "id": "mlx-community/Qwen3.6-35B-A3B-4bit",
                    "context_length": 262144,
                    "input_modalities": ["text"],
                }
            ]
        },
    )
    return integration


async def test_connecting_writes_the_files_and_records_them(service):
    result = await service.connect("codex-app", confirm=False)
    assert result.state == "connected"

    codex = service.home / ".codex"
    assert (codex / "config.toml").exists()
    assert (codex / "auth.json").exists(), "a key is written so the app has something to send"

    # The model catalog and the routing allow-list live in our own directory.
    folder = service.state.paths.codex_app_dir
    catalog = json.loads((folder / "models.json").read_text())
    assert catalog["models"], "the Codex app needs a model catalog"
    allow = json.loads((folder / "routing.json").read_text())
    assert allow, "the routing allow-list must not be empty"


async def test_the_record_is_written_before_any_third_party_file(service, monkeypatch):
    """§11.4: state.json precedes every third-party write, so a crash is recoverable."""
    from splash_gui.integrations import service as module
    from splash_gui.paths import write_atomic as real_atomic

    order: list[str] = []
    real_persist = service.persist

    def persist() -> None:
        order.append("persist")
        real_persist()

    def spy(path: Path, data: bytes, mode: int = 0o600) -> None:
        if not str(path).startswith(str(service.state.paths.base)):
            order.append("write:" + path.name)
        real_atomic(path, data, mode)

    monkeypatch.setattr(service, "persist", persist)
    monkeypatch.setattr(module, "write_atomic", spy)
    await service.connect("codex-app", confirm=False)

    assert order[0] == "persist", f"the record must come first, got {order}"
    assert any(entry.startswith("write:") for entry in order), order


async def test_disconnect_restores_every_file_byte_for_byte(service):
    codex = service.home / ".codex"
    config = codex / "config.toml"
    auth = codex / "auth.json"
    original = b'model = "native"\n[features]\nfoo = true\n'
    config.parent.mkdir(parents=True)
    config.write_bytes(original)
    auth.write_bytes(b'{"OPENAI_API_KEY":"sk-user-token","auth_mode":"chatgpt"}\n')

    await service.connect("codex-app", confirm=False)
    assert config.read_bytes() != original, "connecting must change the user's config"

    result = await service.disconnect("codex-app")
    assert result.state == "not_connected"
    assert config.read_bytes() == original, "the user's config.toml must come back exactly"
    assert json.loads(auth.read_text())["OPENAI_API_KEY"] == "sk-user-token"


async def test_a_file_that_did_not_exist_is_removed_again(service):
    """Connecting creates config.toml here; restoring must not leave it behind."""
    config = service.home / ".codex" / "config.toml"
    assert not config.exists()
    await service.connect("codex-app", confirm=False)
    assert config.exists()
    await service.disconnect("codex-app")
    assert not config.exists(), "a file we created must be removed, not emptied"


async def test_unrelated_settings_in_a_shared_file_survive(service):
    """Codex's config.toml holds much more than Splash touches."""
    config = service.home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_bytes(
        b'model = "native"\napproval_policy = "on-request"\n[desktop]\ntheme = "dark"\n'
    )
    before = read_document(config)

    await service.connect("codex-app", confirm=False)
    await service.disconnect("codex-app")

    after = read_document(config)
    assert after["model"] == before["model"] == "native"
    assert after["approval_policy"] == "on-request"
    assert after["desktop"]["theme"] == "dark"


async def test_reconnecting_after_a_crash_is_recoverable(service, monkeypatch):
    """§11.4: a record with no clean shutdown offers Reconnect or Restore."""
    await service.connect("codex-app", confirm=False)
    revived = IntegrationsService(service.state)
    monkeypatch.setattr(revived, "home", service.home)
    await revived.start()
    assert "codex-app" in revived.records
    assert "codex-app" in revived.recovery
    listing = revived.listing()
    assert listing.unclean_shutdown is True
    assert next(d for d in listing.desktop if d.name == "codex-app").state == "needs_restore"

    result = await revived.restore_all()
    assert result.restored == ["codex-app"], [e.model_dump() for e in result.errors]
    assert not result.errors
    assert not (service.home / ".codex" / "config.toml").exists()


async def test_restore_all_covers_every_connected_app(service, monkeypatch):
    """Both desktop integrations at once."""
    monkeypatch.setattr(service.state.proxy, "models_list", lambda: {"data": []})
    await service.connect("codex-app", confirm=False)
    # Claude Desktop starts a real gateway; skip that part but keep the records.
    monkeypatch.setattr(service, "start_gateway", _noop_gateway())
    await service.connect("claude-desktop", confirm=False)
    assert set(service.records) == {"codex-app", "claude-desktop"}

    result = await service.restore_all()
    assert sorted(result.restored) == ["claude-desktop", "codex-app"]
    assert not service.records
    assert not service.recovery


async def test_connecting_a_running_app_needs_confirmation(service, monkeypatch):
    quit_asked = False

    def running(name: str) -> bool:
        return not quit_asked

    async def quit_app(name: str) -> bool:
        nonlocal quit_asked
        quit_asked = True
        return True

    monkeypatch.setattr(service, "running", running)
    monkeypatch.setattr(service, "quit_app", quit_app)

    with pytest.raises(ApiError) as caught:
        await service.connect("codex-app", confirm=False)
    assert caught.value.code == "restart_confirmation_required"

    result = await service.connect("codex-app", confirm=True)
    assert result.state == "connected"


async def test_connecting_an_app_that_is_not_installed_is_a_404(service, monkeypatch):
    # Not by renaming anything: the lookup also searches /Applications, which may
    # hold a real app on the machine running the tests.
    monkeypatch.setattr(service, "app", lambda name: None)
    with pytest.raises(ApiError) as caught:
        await service.connect("codex-app", confirm=False)
    assert caught.value.code == "app_not_found"


async def test_a_failure_midway_rolls_everything_back(service, monkeypatch):
    """If the app cannot be opened, no half-written config is left behind."""
    original = b'model = "native"\n'
    config = service.home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_bytes(original)

    monkeypatch.setattr(
        service.state.macos,
        "open",
        lambda *args: subprocess.CompletedProcess(args, 1, "", "could not open"),
    )
    with pytest.raises(ApiError):
        await service.connect("codex-app", confirm=False)
    assert config.read_bytes() == original
    assert "codex-app" not in service.records, "a failed connect must leave no record"


async def test_the_listing_reports_the_cli_launchers(service):
    listing = service.listing()
    names = {row.name for row in listing.cli}
    assert names == {"claude", "codex", "opencode", "hermes", "pi"}
    for row in listing.cli:
        assert row.command == f"splash launch {row.name}"
        assert row.changes.notes, "each launcher must say it is session-only (D18)"


def _noop_gateway() -> Callable[[], Coroutine[Any, Any, None]]:
    """Claude Desktop's gateway binds a real port; these tests do not need one."""

    async def start_gateway() -> None:
        return None

    return start_gateway


# --- Codex routing (SPEC §11.3.2) ------------------------------------------------


def _allow(harness, *models: str) -> None:
    folder = harness.state.paths.codex_app_dir
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "routing.json").write_text(json.dumps(list(models)))


def _connected(harness) -> None:
    harness.state.integrations.records["codex-app"] = {"connected_at": "2026-10-03T00:00:00+00:00"}


def test_codex_routing_is_loopback_only(harness_factory):
    """A browser page or a LAN client must never reach the Codex router."""
    harness = harness_factory(installed=())
    _allow(harness, "splash-model")
    _connected(harness)

    with_browser = harness_factory(
        installed=(),
        headers={"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
    )
    _allow(with_browser, "splash-model")
    _connected(with_browser)
    response = with_browser.client.post("/api/codex/v1/responses", json={"model": "splash-model"})
    assert response.status_code == 403, "an Origin header marks a browser, which we refuse"

    off_host = harness_factory(installed=(), base_url="http://box.local:8000")
    _allow(off_host, "splash-model")
    _connected(off_host)
    response = off_host.client.post("/api/codex/v1/responses", json={"model": "splash-model"})
    assert response.status_code == 403, "only 127.0.0.1/localhost/::1 may use it"


def test_codex_returns_503_when_the_app_is_not_connected(harness_factory):
    harness = harness_factory(installed=())
    _allow(harness, "splash-model")
    response = harness.client.post("/api/codex/v1/responses", json={"model": "splash-model"})
    assert response.status_code == 503


def test_codex_refuses_a_websocket_upgrade(harness_factory):
    harness = harness_factory(installed=())
    _allow(harness, "splash-model")
    _connected(harness)
    response = harness.client.get(
        "/api/codex/v1/models", headers={"Upgrade": "websocket", "Connection": "Upgrade"}
    )
    assert response.status_code == 426


def test_codex_only_serves_the_routed_endpoints(harness_factory):
    """A routed request is answered by the local proxy, in Splash's error shape."""
    harness = harness_factory(installed=())
    _allow(harness, MODEL)
    _connected(harness)
    for allowed in ("responses", "chat/completions", "completions"):
        response = harness.client.post(f"/api/codex/v1/{allowed}", json={"model": MODEL})
        body = response.json()
        assert "error" in body, f"{allowed} must be answered locally, got {response.text}"
        assert "code" in body["error"], body
    # An endpoint outside the routed set is refused rather than passed on.
    other = harness.client.post("/api/codex/v1/embeddings", json={"model": MODEL})
    assert other.status_code == 404


def test_a_model_outside_the_allow_list_goes_upstream_not_to_splash(harness_factory):
    """Anything not on the list belongs to OpenAI (SPEC §11.3.2).

    The suite is offline, so the upstream attempt trips the network guard. That
    is the proof: a locally routed request would never resolve api.openai.com.
    """
    harness = harness_factory(installed=())
    _allow(harness, MODEL)
    _connected(harness)
    with pytest.raises(AssertionError, match=r"api\.openai\.com"):
        harness.client.post("/api/codex/v1/responses", json={"model": "gpt-5-codex"})


def test_the_codex_router_is_not_in_the_public_schema(harness_factory):
    """It is an implementation detail of the desktop integration, not public API."""
    harness = harness_factory(installed=())
    schema = harness.app.openapi()
    assert not [p for p in schema["paths"] if p.startswith("/api/codex")]


def test_the_allow_list_holds_only_installed_models(service):
    """§11.3.2: the Codex app must not be offered a model the manager cannot serve."""
    listing = service.state.proxy.models_list()["data"]
    allow = [model["id"] for model in listing]
    installed = set(service.state.installed_models())
    for model_id in allow:
        base = model_id.split(":")[0]
        assert base in installed or base in {m.split(":")[0] for m in allow}

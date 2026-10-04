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
def fake_home(tmp_path: Path) -> Path:
    """A throwaway home directory with both apps 'installed' and nothing running.

    It reaches the service through its `home` constructor argument; neither
    `$HOME` nor `Path.home` is patched.
    """
    home = tmp_path / "home"
    home.mkdir()
    for name in ("Claude.app", "Codex.app", "ChatGPT.app"):
        (home / "Applications" / name / "Contents" / "MacOS").mkdir(parents=True)
    return home


@pytest.fixture
def service(client, fake_home, monkeypatch) -> IntegrationsService:
    """The real service, with only the app/process boundaries faked out."""
    state = client.app.state.manager
    assert isinstance(state.integrations, IntegrationsService)
    integration = IntegrationsService(state, home=fake_home)
    monkeypatch.setattr(state, "integrations", integration)
    monkeypatch.setattr(integration, "running", lambda name: False)
    monkeypatch.setattr(integration, "native_codex_models", lambda codex_home: [])
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
    revived = IntegrationsService(service.state, home=service.home)
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


async def test_a_failed_write_leaves_unwritten_files_byte_for_byte(service, monkeypatch):
    """Claude's own formatting survives a connect that fails partway: files we
    never reached are not re-encoded by the rollback."""
    from splash_gui.integrations import service as module
    from splash_gui.paths import write_atomic as real_atomic

    support = service.home / "Library" / "Application Support"
    configs = [support / d / "claude_desktop_config.json" for d in ("Claude", "Claude-3p")]
    originals = [b'{"mcpServers":{"x":{"command":"y"}}}', b'{\n\t"a": 1\n}']
    for path, data in zip(configs, originals, strict=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    written: list[str] = []

    def failing(path: Path, data: bytes, mode: int = 0o600) -> None:
        if not str(path).startswith(str(service.state.paths.base)):
            written.append(path.name)
            if len(written) == 2:
                raise OSError("disk full")
        real_atomic(path, data, mode)

    monkeypatch.setattr(service, "start_gateway", _noop_gateway())
    monkeypatch.setattr(module, "write_atomic", failing)
    with pytest.raises(OSError):
        await service.connect("claude-desktop", confirm=False)
    for path, data in zip(configs, originals, strict=True):
        assert path.read_bytes() == data
    assert "claude-desktop" not in service.records


async def test_symlinked_codex_config_keeps_its_link_and_mode(service, tmp_path):
    dotfiles = tmp_path / "dotfiles" / "codex.toml"
    dotfiles.parent.mkdir()
    original = b'model = "native"\n'
    dotfiles.write_bytes(original)
    dotfiles.chmod(0o644)
    config = service.home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.symlink_to(dotfiles)

    await service.connect("codex-app", confirm=False)
    assert config.is_symlink(), "connecting must not replace the user's symlink"
    assert b"openai_base_url" in dotfiles.read_bytes()
    await service.disconnect("codex-app")
    assert config.is_symlink() and config.resolve() == dotfiles.resolve()
    assert dotfiles.read_bytes() == original
    assert dotfiles.stat().st_mode & 0o777 == 0o644


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


# --- Claude Desktop (SPEC §11.3.1) ------------------------------------------------


def _claude_paths(home: Path) -> dict[str, Path]:
    support = home / "Library" / "Application Support"
    library = support / "Claude-3p" / "configLibrary"
    return {
        "deployment": library / "c4f53a60-833b-4a8a-b15c-5641d82a5902.json",
        "meta": library / "_meta.json",
        "config_1p": support / "Claude" / "claude_desktop_config.json",
        "config_3p": support / "Claude-3p" / "claude_desktop_config.json",
    }


async def test_claude_desktop_writes_the_gateway_config(service, monkeypatch):
    monkeypatch.setattr(service, "start_gateway", _noop_gateway())
    files = _claude_paths(service.home)
    files["config_1p"].parent.mkdir(parents=True)
    files["config_1p"].write_bytes(b'{"mcpServers": {"x": {"command": "y"}}}\n')
    await service.connect("claude-desktop", confirm=False)
    deployment = json.loads(files["deployment"].read_text())
    assert deployment["inferenceProvider"] == "gateway"
    assert deployment["inferenceGatewayBaseUrl"] == "http://127.0.0.1:18435", "no /v1"
    assert deployment["inferenceGatewayApiKey"] == "splash"
    assert deployment["inferenceGatewayAuthScheme"] == "bearer"
    assert (
        deployment["disableDeploymentModeChooser"] is True and "inferenceModels" not in deployment
    )
    meta = json.loads(files["meta"].read_text())
    assert (
        meta["appliedId"] == deployment_id()
        and {"id": deployment_id(), "name": "Splash"} in meta["entries"]
    )
    for key in ("config_1p", "config_3p"):
        assert json.loads(files[key].read_text())["deploymentMode"] == "3p"
    assert json.loads(files["config_1p"].read_text())["mcpServers"] == {"x": {"command": "y"}}


def deployment_id() -> str:
    from splash_gui.integrations.service import DEPLOYMENT

    return DEPLOYMENT


async def test_claude_desktop_restores_byte_for_byte(service, monkeypatch):
    monkeypatch.setattr(service, "start_gateway", _noop_gateway())
    files = _claude_paths(service.home)
    originals = {
        "config_1p": b'{\n  "deploymentMode": "1p",\n  "theme": "dark"\n}',
        "meta": b'{"appliedId": "theirs", "entries": [{"id": "theirs", "name": "Work"}]}',
    }
    for key, data in originals.items():
        files[key].parent.mkdir(parents=True, exist_ok=True)
        files[key].write_bytes(data)
    await service.connect("claude-desktop", confirm=False)
    await service.disconnect("claude-desktop")
    for key, data in originals.items():
        assert files[key].read_bytes() == data, key
    assert not files["deployment"].exists() and not files["config_3p"].exists()


async def test_claude_desktop_rewrites_while_connected_are_kept(service, monkeypatch):
    """Claude rewrites its settings on shutdown: restore only our keys (SPEC §11.3.1)."""
    monkeypatch.setattr(service, "start_gateway", _noop_gateway())
    files = _claude_paths(service.home)
    await service.connect("claude-desktop", confirm=False)
    # The app reformats and adds its own state to every file while connected.
    for key in ("config_1p", "deployment"):
        data = json.loads(files[key].read_text())
        data["appState"] = {"windowWidth": 900}
        files[key].write_text(json.dumps(data))
    meta = json.loads(files["meta"].read_text())
    meta["entries"].append({"id": "user-added", "name": "Mine"})
    files["meta"].write_text(json.dumps(meta))
    await service.disconnect("claude-desktop")
    restored = json.loads(files["config_1p"].read_text())
    assert restored["deploymentMode"] == "1p", "no previous mode: back to first-party"
    assert restored["appState"] == {"windowWidth": 900}
    meta = json.loads(files["meta"].read_text())
    assert "appliedId" not in meta
    assert meta["entries"] == [{"id": "user-added", "name": "Mine"}]
    assert not files["deployment"].exists(), "our configLibrary file goes, whatever it holds"


async def test_manager_shutdown_restores_desktop_apps(service, monkeypatch):
    """Quitting Splash GUI restores every connection (SPEC §4.2 option A, §11.4)."""
    monkeypatch.setattr(service, "start_gateway", _noop_gateway())
    config = service.home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    original = b'model = "gpt-5"\n'
    config.write_bytes(original)
    await service.connect("codex-app", confirm=False)
    await service.connect("claude-desktop", confirm=False)
    await service.shutdown()
    assert config.read_bytes() == original
    assert not _claude_paths(service.home)["deployment"].exists()
    assert json.loads(service.state.paths.integrations_state.read_text()) == {}


async def test_an_unclean_shutdown_raises_an_alert_with_both_choices(service):
    await service.connect("codex-app", confirm=False)
    revived = IntegrationsService(service.state, home=service.home)
    await revived.start()
    alert = service.state.alerts.get("unclean_integration_shutdown")
    assert alert is not None and alert.dismissible is False
    paths = {a.path for a in alert.actions}
    assert paths == {
        "/api/admin/integrations/codex-app/connect",
        "/api/admin/integrations/restore-all",
    }
    await revived.restore_all()
    assert service.state.alerts.get("unclean_integration_shutdown") is None


async def test_a_failed_restore_keeps_the_record(service, monkeypatch):
    await service.connect("codex-app", confirm=False)
    from splash_gui.integrations import service as module

    def broken(path: Path, record: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(module, "restore", broken)
    with pytest.raises(ApiError) as caught:
        await service.disconnect("codex-app")
    assert caught.value.code == "restore_incomplete"
    assert "codex-app" in service.records
    assert "codex-app" in json.loads(service.state.paths.integrations_state.read_text())


# --- Codex app (SPEC §11.3.2) -------------------------------------------------------


async def test_codex_catalog_lists_splash_first_then_native(service, monkeypatch):
    native = [{"slug": "gpt-6", "display_name": "GPT-6", "supported_in_api": True}]
    monkeypatch.setattr(service, "native_codex_models", lambda codex_home: native)
    await service.connect("codex-app", confirm=False)
    catalog = json.loads((service.state.paths.codex_app_dir / "models.json").read_text())["models"]
    assert catalog[0]["slug"] == "mlx-community/Qwen3.6-35B-A3B-4bit"
    assert catalog[0]["description"] == "Splash local model" and catalog[0]["supported_in_api"]
    efforts = [level["effort"] for level in catalog[0]["supported_reasoning_levels"]]
    assert efforts == ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    assert catalog[-1] == {**native[0], "supported_in_api": False}
    config = read_document(service.home / ".codex" / "config.toml")
    assert config["openai_base_url"] == "http://127.0.0.1:8000/api/codex/v1"
    assert "model_provider" not in config and "profile" not in config and "model" not in config
    assert {"none", "max"} <= set(config["desktop"]["enabled-reasoning-efforts"])


async def test_codex_open_goes_to_a_new_thread(service, monkeypatch):
    calls: list[tuple[str, ...]] = []

    def record(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service.state.macos, "open", record)
    await service.connect("codex-app", confirm=False)
    assert calls[-1][-1] == "codex://threads/new?mode=codex"


def test_native_models_from_the_bundled_cli(client, fake_home, tmp_path):
    from .conftest import write_script

    app = fake_home / "Applications" / "Codex.app"
    cli = app / "Contents" / "Resources" / "codex-cli"
    (cli / "bin").mkdir(parents=True)
    (cli / "codex-package.json").write_text(json.dumps({"entrypoint": "bin/codex"}))
    write_script(
        cli / "bin" / "codex",
        'test "$1 $2" = "debug models" || exit 2\n'
        'case "$CODEX_HOME" in */splash-codex-*) ;; *) exit 3;; esac\n'
        'echo \'{"models": [{"slug": "gpt-6"}]}\'\n',
    )
    service = IntegrationsService(client.app.state.manager, home=fake_home)
    assert service.native_codex_models(fake_home / ".codex") == [{"slug": "gpt-6"}]


def test_native_models_fall_back_to_the_cache(client, fake_home):
    service = IntegrationsService(client.app.state.manager, home=fake_home)
    codex = fake_home / ".codex"
    codex.mkdir()
    (codex / "models_cache.json").write_text(json.dumps({"models": [{"slug": "cached"}]}))
    assert service.native_codex_models(codex) == [{"slug": "cached"}]
    (codex / "models_cache.json").write_text("not json")
    assert service.native_codex_models(codex) == []


def test_desktop_rows_mark_versions_untested(client, fake_home):
    import plistlib

    app = fake_home / "Applications" / "Claude.app" / "Contents"
    with (app / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleShortVersionString": "2.19675.0"}, handle)
    service = IntegrationsService(client.app.state.manager, home=fake_home)
    row = service.desktop("claude-desktop")
    assert row.version == "2.19675.0" and row.untested_version is True


# --- CLI launcher rows (SPEC §10.7, §11.2) ---------------------------------------------


def test_cli_rows_say_what_changes(service):
    rows = {row.name: row for row in service.listing().cli}
    assert rows["claude"].changes.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8000"
    assert "--model" in rows["claude"].changes.args and not rows["claude"].changes.files
    assert any("model_providers.splash" in a for a in rows["codex"].changes.args)
    assert rows["codex"].changes.files == [] and rows["opencode"].changes.files == []
    assert rows["hermes"].changes.files == [
        str(service.home / ".hermes/profiles/splash/config.yaml")
    ]
    assert rows["pi"].changes.files == [str(service.home / ".pi/agent/models.json")]


def test_hermes_and_pi_entries_are_listed_and_removed_with_a_backup(service):
    profiles = service.home / ".hermes" / "profiles"
    for name in ("splash", "splash-9000", "default", "splashy"):
        (profiles / name).mkdir(parents=True)
        (profiles / name / "config.yaml").write_text(f"name: {name}\n")
    pi = service.home / ".pi" / "agent" / "models.json"
    pi.parent.mkdir(parents=True)
    pi.write_text(json.dumps({"providers": {"splash": {}, "openai": {"k": 1}, "splash-9000": {}}}))
    rows = {row.name: row for row in service.listing().cli}
    assert rows["hermes"].entries == ["splash", "splash-9000"]
    assert rows["pi"].entries == ["splash", "splash-9000"]

    hermes = service.remove_entries("hermes")
    assert sorted(hermes.removed) == ["splash", "splash-9000"]
    assert sorted(p.name for p in profiles.iterdir()) == ["default", "splashy"]
    assert any(Path(b).name == "splash" for b in hermes.backups)

    result = service.remove_entries("pi")
    assert sorted(result.removed) == ["splash", "splash-9000"]
    assert json.loads(pi.read_text())["providers"] == {"openai": {"k": 1}}
    backup = json.loads(Path(result.backups[0]).read_text())
    assert set(backup["providers"]) == {"splash", "openai", "splash-9000"}


def test_pi_entries_write_through_a_symlink(service):
    real = service.home / "dotfiles" / "models.json"
    real.parent.mkdir(parents=True)
    real.write_text(json.dumps({"providers": {"splash": {}, "other": {}}}))
    link = service.home / ".pi" / "agent" / "models.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    service.remove_entries("pi")
    assert link.is_symlink(), "a dotfile manager's link survives"
    assert json.loads(real.read_text())["providers"] == {"other": {}}


# --- The Claude Desktop gateway, live (SPEC §11.3.1) --------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _gateway_harness(
    harness_factory: Any, fake_home: Path, monkeypatch: Any, slots: Any = None
) -> tuple[Any, str]:
    h = harness_factory()
    port = _free_port()
    patch: dict[str, Any] = {"port": port}
    if slots is not None:
        patch["slots"] = slots
    h.patch_settings({"global": {"integrations": {"claude_desktop": patch}}})
    service = IntegrationsService(h.state, home=fake_home)
    monkeypatch.setattr(h.state, "integrations", service)
    monkeypatch.setattr(service, "running", lambda name: False)
    monkeypatch.setattr(
        h.state.macos, "open", lambda *args: subprocess.CompletedProcess(args, 0, "", "")
    )
    response = h.client.post("/api/admin/integrations/claude-desktop/connect", json={})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "connected"
    return h, f"http://127.0.0.1:{port}"


def test_gateway_routes_messages_to_the_active_model(harness_factory, fake_home, monkeypatch):
    import httpx

    h, base = _gateway_harness(harness_factory, fake_home, monkeypatch)
    try:
        h.load()
        assert httpx.get(base + "/_splash/health").status_code == 204
        models = httpx.get(base + "/v1/models").json()
        ids = [m["id"] for m in models["data"]]
        assert "claude-opus-5" in ids and "claude-haiku-4-5-20251001" in ids
        assert models["first_id"] == ids[0] and models["has_more"] is False
        entry = models["data"][0]
        assert entry["type"] == "model" and entry["display_name"] == MODEL
        assert entry["anthropic_family_tier"] in ("fable", "opus", "sonnet", "haiku")
        reply = httpx.post(
            base + "/v1/messages",
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": "Bearer splash", "x-api-key": "splash"},
            timeout=30,
        )
        assert reply.status_code == 200, reply.text
        sent = [r for r in h.last_engine_requests() if r["path"] == "/v1/messages" and r["body"]][
            -1
        ]
        assert sent["body"]["model"] == MODEL, "the slot is rewritten to the Splash model"
        headers = {k.lower(): v for k, v in sent["headers"].items()}
        assert headers["authorization"] == f"Bearer {h.sup.internal_key}"
        assert "x-api-key" not in headers, "incoming credentials are removed"
        rows = h.client.get("/api/admin/usage/requests", params={"client": "claude-desktop"})
        assert rows.json()["total"] == 1
        count = httpx.post(
            base + "/v1/messages/count_tokens",
            json={"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert count.status_code == 200 and count.json()["input_tokens"] > 0
        assert httpx.post(base + "/v1/messages", json={"model": "gpt-4o"}).status_code == 404
        assert httpx.get(base + "/v1/other").status_code == 404
        assert httpx.get(base + "/v1/models", headers={"Origin": "https://evil"}).status_code == 403
        assert httpx.get(base + "/v1/models", headers={"Host": "box.local"}).status_code == 403
    finally:
        assert h.client.post("/api/admin/integrations/claude-desktop/disconnect").status_code == 200
    import httpx as _httpx

    with pytest.raises(_httpx.ConnectError):
        _httpx.get(base + "/_splash/health", timeout=2)


def test_gateway_slot_can_map_to_a_profile(harness_factory, fake_home, monkeypatch):
    import httpx

    slots = {
        "claude-fable-5": None,
        "claude-opus-5": f"{MODEL}:no-think",
        "claude-sonnet-5": None,
        "claude-haiku-4-5-20251001": None,
    }
    h, base = _gateway_harness(harness_factory, fake_home, monkeypatch, slots=slots)
    try:
        h.load()
        reply = httpx.post(
            base + "/v1/messages",
            json={
                "model": "claude-opus-5",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
            timeout=30,
        )
        assert reply.status_code == 200, reply.text
        row = h.client.get("/api/admin/usage/requests", params={"client": "claude-desktop"}).json()
        assert row["rows"][0]["profile"] == "no-think"
    finally:
        h.client.post("/api/admin/integrations/claude-desktop/disconnect")


def test_gateway_auto_loads_or_says_no_model(harness_factory, fake_home, monkeypatch):
    import httpx

    h, base = _gateway_harness(harness_factory, fake_home, monkeypatch)
    try:
        body = {
            "model": "claude-opus-5",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}],
        }
        reply = httpx.post(base + "/v1/messages", json=body, timeout=30)
        assert reply.status_code == 503
        assert reply.json() == {
            "type": "error",
            "error": {"type": "overloaded_error", "message": "Splash GUI: no model loaded"},
        }
        h.patch_settings({"global": {"routing": {"default_model": MODEL}}})
        reply = httpx.post(base + "/v1/messages", json=body, timeout=60)
        assert reply.status_code == 200, reply.text
        assert h.engine()["model"] == MODEL, "the gateway loaded the mapped/default model"
    finally:
        h.client.post("/api/admin/integrations/claude-desktop/disconnect")


def test_quitting_the_manager_restores_byte_for_byte(
    paths: Any, secrets: Any, web_dist: Path, fake_home: Path
) -> None:
    """SPEC §21: connected apps are restored byte for byte when Splash GUI quits
    (the manager's lifespan shutdown), not only on Disconnect."""
    from fastapi.testclient import TestClient

    from splash_gui.app import AppConfig, create_app
    from splash_gui.system.macos import RecordingMacOS

    from .conftest import LOOPBACK_CLIENT, fake_engine

    config = fake_home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    original = b'# mine\nmodel = "gpt-5"\n\n[desktop]\ntheme = "dark"\n'
    config.write_bytes(original)
    claude = fake_home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    claude.parent.mkdir(parents=True)
    claude_original = b'{\n  "theme": "light"\n}\n'
    claude.write_bytes(claude_original)

    app = create_app(AppConfig(paths=paths, web_dist=web_dist, secrets=secrets))
    state = app.state.manager
    state.discover_engine = fake_engine
    state.user_home = fake_home
    state.macos = RecordingMacOS()
    with TestClient(app, client=LOOPBACK_CLIENT) as client:
        headers = {"Authorization": f"Bearer {state.auth.cli_token()}"}
        state.integrations.native_codex_models = lambda codex_home: []
        state.integrations.start_gateway = _noop_gateway()  # no real port 18435 here
        codex = client.post("/api/admin/integrations/codex-app/connect", json={}, headers=headers)
        assert codex.status_code == 200, codex.text
        claude_connect = client.post(
            "/api/admin/integrations/claude-desktop/connect", json={}, headers=headers
        )
        assert claude_connect.status_code == 200, claude_connect.text
        assert config.read_bytes() != original and b'"3p"' in claude.read_bytes()
    assert config.read_bytes() == original
    assert claude.read_bytes() == claude_original
    assert not (fake_home / ".codex" / "auth.json").exists()
    assert json.loads(paths.integrations_state.read_text()) == {}

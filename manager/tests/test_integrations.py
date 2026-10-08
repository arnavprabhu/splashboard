"""Desktop integrations and Codex routing (SPEC §11, D18).

The guarantee under test is §11.4: connecting writes a restore record *before*
any third-party file is touched, and disconnecting puts every file back exactly
as it was, including the parts Splash never intended to change. Nothing here
talks to a real Claude or ChatGPT app: `osascript`, `pgrep` and the app lookup
are stubbed, and the filesystem under test is a throwaway `$HOME`.
"""

from __future__ import annotations

import ast
import json
import plistlib
import subprocess
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from splash_gui.errors import ApiError
from splash_gui.integrations.service import IntegrationsService
from splash_gui.integrations.snapshots import read_document
from splash_gui.secrets import SecretName, redact_text

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
        lambda label, bundle_id=None: subprocess.CompletedProcess(["osascript"], 0, "", ""),
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
    token = service.state.secrets.get(SecretName.CODEX_ROUTER)
    assert token and token.encode() in config.read_bytes()

    result = await service.disconnect("codex-app")
    assert result.state == "not_connected"
    assert service.state.secrets.get(SecretName.CODEX_ROUTER) is None, "D58: token revoked"
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
    assert service.state.secrets.get(SecretName.CODEX_ROUTER) is None
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


async def test_codex_quit_falls_back_to_the_bundle_id(service, monkeypatch):
    """SPEC §11.3.2: quitting the Codex app by name can fail; it then quits by bundle id."""
    from splash_gui.integrations.service import CODEX_BUNDLE_ID

    calls: list[tuple[str, str | None]] = []
    stopped = False

    def quit_app(label: str, bundle_id: str | None = None) -> subprocess.CompletedProcess:
        nonlocal stopped
        calls.append((label, bundle_id))
        ok = bundle_id == CODEX_BUNDLE_ID
        stopped = stopped or ok
        return subprocess.CompletedProcess(["osascript"], 0 if ok else 1, "", "")

    monkeypatch.setattr(service.state.macos, "quit_app", quit_app)
    monkeypatch.setattr(service, "running", lambda name: not stopped)

    assert await service.quit_app("codex-app") is True
    assert len(calls) == 2
    assert calls[0][1] is None, "the name is tried first"
    assert calls[1] == (calls[0][0], CODEX_BUNDLE_ID)


def _app_bundle(root: Path, name: str, info: bytes | None) -> Path:
    """A fake app bundle; `info` is the raw Info.plist, or None for no plist at all."""
    app = root / name
    (app / "Contents").mkdir(parents=True)
    if info is not None:
        (app / "Contents" / "Info.plist").write_bytes(info)
    return app


def _quit_recorder(monkeypatch, service, accepted: str | None) -> list[tuple[str, str | None]]:
    """Fakes osascript: quitting by name fails, and quitting by `accepted` id succeeds."""
    calls: list[tuple[str, str | None]] = []
    stopped = False

    def quit_app(label: str, bundle_id: str | None = None) -> subprocess.CompletedProcess:
        nonlocal stopped
        calls.append((label, bundle_id))
        ok = accepted is not None and bundle_id == accepted
        stopped = stopped or ok
        return subprocess.CompletedProcess(["osascript"], 0 if ok else 1, "", "")

    monkeypatch.setattr(service.state.macos, "quit_app", quit_app)
    monkeypatch.setattr(service, "running", lambda name: not stopped)
    return calls


async def test_quit_falls_back_to_the_bundle_id_of_the_app_found(service, monkeypatch, tmp_path):
    """SPEC §11.3.2: the fallback is the located app's own CFBundleIdentifier, not a fixed one."""
    plist = plistlib.dumps({"CFBundleIdentifier": "com.example.chatgpt"})
    app = _app_bundle(tmp_path, "ChatGPT.app", plist)
    monkeypatch.setattr(service, "app", lambda name: app)
    calls = _quit_recorder(monkeypatch, service, accepted="com.example.chatgpt")

    assert await service.quit_app("codex-app") is True
    assert calls == [("ChatGPT", None), ("ChatGPT", "com.example.chatgpt")]


@pytest.mark.parametrize(
    ("name", "info", "expected"),
    [
        # An unreadable plist: only Codex.app keeps the Codex id as its fallback.
        ("Codex.app", None, "com.openai.codex"),
        ("Codex.app", b"not a plist", "com.openai.codex"),
        ("ChatGPT.app", None, None),
        ("ChatGPT.app", b"not a plist", None),
        # A plist with no bundle id is unreadable for this purpose too.
        ("ChatGPT.app", plistlib.dumps({"CFBundleName": "ChatGPT"}), None),
    ],
)
async def test_quit_fallback_when_the_bundle_id_is_unreadable(
    service, monkeypatch, tmp_path, name, info, expected
):
    app = _app_bundle(tmp_path, name, info)
    monkeypatch.setattr(service, "app", lambda kind: app)
    calls = _quit_recorder(monkeypatch, service, accepted=expected)

    if expected is None:
        with pytest.raises(ApiError) as caught:
            await service.quit_app("codex-app")
        assert caught.value.code == "app_quit_failed"
        assert calls == [(app.stem, None)], "no bundle id is guessed for ChatGPT.app"
    else:
        assert await service.quit_app("codex-app") is True
        assert calls[-1] == (app.stem, expected)


async def test_a_failed_quit_by_bundle_id_is_reported(service, monkeypatch):
    monkeypatch.setattr(
        service.state.macos,
        "quit_app",
        lambda label, bundle_id=None: subprocess.CompletedProcess(["osascript"], 1, "", ""),
    )
    monkeypatch.setattr(service, "running", lambda name: True)
    with pytest.raises(ApiError) as caught:
        await service.quit_app("codex-app")
    assert caught.value.code == "app_quit_failed"


def test_running_matches_the_app_binaries_only(service, monkeypatch):
    """The Codex and ChatGPT app binaries match; helpers and CLIs do not."""
    import re

    patterns: list[str] = []

    def pgrep(*args: str, **kwargs: Any) -> list[int]:
        patterns.append(args[-1])
        return []

    monkeypatch.setattr(service.state.macos, "pgrep", pgrep)
    IntegrationsService.running(service, "codex-app")
    (pattern,) = patterns
    assert re.search(pattern, "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT")
    assert re.search(pattern, "/Applications/Codex.app/Contents/MacOS/Codex")
    assert not re.search(pattern, "/Applications/ChatGPT Helper (Renderer).app/Contents/MacOS/x")
    assert not re.search(pattern, "/opt/homebrew/bin/codex")


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


def test_codex_refuses_a_websocket_upgrade_off_responses(harness_factory):
    """D63: only `/v1/responses` has a WebSocket form; 426 sends Codex to HTTP."""
    harness = harness_factory(installed=())
    _allow(harness, "splash-model")
    _connected(harness)
    response = harness.client.get(
        "/api/codex/v1/models", headers={"Upgrade": "websocket", "Connection": "Upgrade"}
    )
    assert response.status_code == 426
    token = harness.state.secrets.generate(SecretName.CODEX_ROUTER, prefix="")
    with (
        pytest.raises(WebSocketDenialResponse) as refused,
        harness.client.websocket_connect(f"{WS}/api/codex/t/{token}/v1/models"),
    ):
        pass
    assert refused.value.status_code == 426


# --- Codex router: the Responses WebSocket (D63) ----------------------------------

# TestClient.websocket_connect joins a bare path onto ws://testserver.
WS = "ws://127.0.0.1:8000"


def _ws_ready(harness_factory, text: str = "Hello from the fake engine."):
    """A loaded fake engine, the Codex app connected, and the router token."""
    h = harness_factory()
    assert h.load()["state"] == "ready"
    h.fake("POST", "/_fake/mode", {"config": {"text": text}})
    _allow(h, MODEL)
    _connected(h)
    token = h.state.secrets.generate(SecretName.CODEX_ROUTER, prefix="")
    return h, f"{WS}/api/codex/t/{token}/v1/responses"


def _engine_responses(h: Any) -> list[dict[str, Any]]:
    return [r["body"] for r in h.last_engine_requests() if r["path"] == "/v1/responses"]


def _create(text: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "response.create",
        "model": MODEL,
        "instructions": "You are Codex.",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
        ],
        "stream": True,
        "store": False,
        **extra,
    }


def _turn(ws: Any) -> list[dict[str, Any]]:
    """Frames up to and including the one that ends the response."""
    frames: list[dict[str, Any]] = []
    while True:
        frame = json.loads(ws.receive_text())
        frames.append(frame)
        if frame["type"] in (
            "response.completed",
            "response.incomplete",
            "response.failed",
            "error",
        ):
            return frames


def _text(frames: list[dict[str, Any]]) -> str:
    return "".join(f["delta"] for f in frames if f["type"] == "response.output_text.delta")


CODEX_TOOLS = [
    {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
    {"type": "web_search", "external_web_access": False},
]


def test_codex_websocket_streams_a_splash_model(harness_factory):
    h, url = _ws_ready(harness_factory)
    with h.client.websocket_connect(url) as ws:
        ws.send_text(json.dumps(_create("say hi", tools=CODEX_TOOLS)))
        frames = _turn(ws)
    kinds = [f["type"] for f in frames]
    assert kinds[0] == "response.created"
    assert kinds[-1] == "response.completed"
    assert "response.output_item.done" in kinds
    assert _text(frames) == "Hello from the fake engine."
    assert frames[-1]["response"]["id"] == frames[0]["response"]["id"]
    # The same code path as HTTP: the request reached the engine with the Codex body,
    # and usage recorded it as a complete codex-app request.
    (sent,) = _engine_responses(h)
    assert "type" not in sent and sent["stream"] is True
    # Splash refuses OpenAI's hosted tools with a 400, so they stay with the app.
    assert sent["tools"] == CODEX_TOOLS[:1]
    deadline = time.monotonic() + 5
    while not (rows := h.client.get("/api/admin/usage/requests").json()["rows"]):
        assert time.monotonic() < deadline, "no usage row"
        time.sleep(0.02)
    assert (rows[0]["client"], rows[0]["status"]) == ("codex-app", 200)


def test_codex_http_drops_hosted_tools_for_splash_models(harness_factory):
    """Codex sends `web_search` with every request; Splash 1.3.0 runs function tools only."""
    h, url = _ws_ready(harness_factory)
    body = {k: v for k, v in _create("hi", tools=CODEX_TOOLS).items() if k != "type"}
    response = h.client.post(url.replace(WS, ""), json=body)
    assert response.status_code == 200
    (sent,) = _engine_responses(h)
    assert sent["tools"] == CODEX_TOOLS[:1]


def test_codex_websocket_runs_turns_on_one_connection(harness_factory):
    """A prewarm, a turn, then a continuation that sends only the new items."""
    h, url = _ws_ready(harness_factory)
    with h.client.websocket_connect(url) as ws:
        ws.send_text(json.dumps(_create("first", generate=False)))
        warm = _turn(ws)
        assert [f["type"] for f in warm] == ["response.created", "response.completed"]
        assert warm[-1]["response"]["output"] == []
        assert _engine_responses(h) == [], "a prewarm never reaches the engine"

        ws.send_text(
            json.dumps(_create("", previous_response_id=warm[-1]["response"]["id"], input=[]))
        )
        first = _turn(ws)
        assert first[-1]["type"] == "response.completed"
        assert _text(first) == "Hello from the fake engine."

        follow_up = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "and again"}],
        }
        ws.send_text(
            json.dumps(
                _create("", previous_response_id=first[-1]["response"]["id"], input=[follow_up])
            )
        )
        second = _turn(ws)
        assert second[-1]["type"] == "response.completed"
        assert _text(second) == "Hello from the fake engine."
    engine = _engine_responses(h)
    assert len(engine) == 2
    assert "previous_response_id" not in engine[1]
    # The second request carries the whole conversation: the first input, the items
    # the first answer produced, then the new item.
    answer = [f["item"] for f in first if f["type"] == "response.output_item.done"]
    assert answer and answer[-1]["role"] == "assistant"
    assert engine[1]["input"] == [*engine[0]["input"], *answer, follow_up]
    assert engine[0]["input"][0]["content"][0]["text"] == "first"


def test_codex_websocket_interrupt_stops_the_engine(harness_factory):
    """codex-rs sends `response.interrupt` and reads on until the response ends."""
    h, url = _ws_ready(harness_factory)
    h.fake(
        "POST",
        "/_fake/mode",
        {"config": {"text": None, "reply_tokens": 4000, "tokens_per_second": 40}},
    )
    with h.client.websocket_connect(url) as ws:
        ws.send_text(json.dumps(_create("write a long story")))
        while json.loads(ws.receive_text())["type"] != "response.output_text.delta":
            pass
        ws.send_text(json.dumps({"type": "response.interrupt", "mode": "discard_partial_items"}))
        frames = _turn(ws)
    assert frames[-1]["type"] == "response.incomplete"
    assert frames[-1]["response"]["incomplete_details"] == {"reason": "interrupted"}
    deadline = time.monotonic() + 5
    while h.engine()["requests_in_flight"]:
        assert time.monotonic() < deadline, "the engine request was never cancelled"
        time.sleep(0.02)


def test_the_router_token_is_redacted_from_logs():
    """uvicorn logs each WebSocket handshake path, which holds the token (D58)."""
    line = '127.0.0.1:5000 - "WebSocket /api/codex/t/9f8e7d6c5b4a/v1/responses" [accepted]'
    assert "9f8e7d6c5b4a" not in redact_text(line)


def test_codex_websocket_unknown_previous_response(harness_factory):
    """codex-rs retries the full request when it reads previous_response_not_found."""
    h, url = _ws_ready(harness_factory)
    with h.client.websocket_connect(url) as ws:
        ws.send_text(json.dumps(_create("hi", previous_response_id="resp_unknown")))
        frames = _turn(ws)
    assert frames == [
        {
            "type": "error",
            "status": 400,
            "error": {
                "type": "invalid_request_error",
                "code": "previous_response_not_found",
                "message": "Previous response with id 'resp_unknown' not found.",
            },
        }
    ]


def test_codex_websocket_refuses_a_wrong_token(harness_factory):
    h, _ = _ws_ready(harness_factory)
    with (
        pytest.raises(WebSocketDenialResponse) as refused,
        h.client.websocket_connect(f"{WS}/api/codex/t/not-the-token/v1/responses"),
    ):
        pass
    assert refused.value.status_code == 401
    assert refused.value.json()["error"]["code"] == "invalid_router_token"
    # The legacy path without the API key: the socket opens, a Splash model is refused.
    with h.client.websocket_connect(
        f"{WS}/api/codex/v1/responses", headers={"Authorization": ""}
    ) as ws:
        ws.send_text(json.dumps(_create("hi")))
        frames = _turn(ws)
    assert frames[-1]["type"] == "error"
    assert frames[-1]["status"] == 401
    assert frames[-1]["error"]["type"] == "authentication_error"


def test_codex_websocket_is_loopback_only(harness_factory):
    h, url = _ws_ready(harness_factory)
    lan = TestClient(h.app, base_url="http://127.0.0.1:8000", client=("192.168.1.20", 50000))
    with lan, pytest.raises(WebSocketDenialResponse) as refused, lan.websocket_connect(url):
        pass
    assert refused.value.status_code == 403
    # A browser page (Origin header) is refused too: no cross-site WebSocket.
    with (
        pytest.raises(WebSocketDenialResponse) as browser,
        h.client.websocket_connect(url, headers={"Origin": "http://evil.example"}),
    ):
        pass
    assert browser.value.status_code == 403


def test_codex_websocket_engine_errors_become_error_frames(harness_factory):
    h, url = _ws_ready(harness_factory)
    h.fake("POST", "/_fake/mode", {"mode": "queue_full"})
    with h.client.websocket_connect(url) as ws:
        ws.send_text(json.dumps(_create("hi")))
        frames = _turn(ws)
        assert frames == [
            {
                "type": "error",
                "status": 503,
                "error": {
                    "message": "frontend request capacity is exhausted",
                    "type": "server_error",
                    "code": "frontend_overloaded",
                },
                "headers": {"retry-after": "1"},
            }
        ]
        # A failure after the stream began becomes response.failed, which codex-rs reads.
        h.fake("POST", "/_fake/mode", {"mode": "fail_midstream"})
        ws.send_text(json.dumps(_create("hi")))
        failed = _turn(ws)
    assert failed[0]["type"] == "response.created"
    assert failed[-1]["type"] == "response.failed"
    assert failed[-1]["response"]["status"] == "failed"
    assert failed[-1]["response"]["error"]["code"]
    assert failed[-1]["response"]["id"] == failed[0]["response"]["id"]


def test_codex_websocket_sends_native_models_upstream(harness_factory, monkeypatch):
    """A model off the allow list goes upstream over HTTP with the caller's own
    credentials, as on the HTTP route, and its SSE events come back as frames."""
    h, url = _ws_ready(harness_factory)
    seen: list[httpx.Request] = []
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": {"id": "resp_up"}},
        {"type": "response.output_text.delta", "delta": "from upstream"},
        {"type": "response.completed", "response": {"id": "resp_up", "usage": None}},
    ]

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(body.encode()),
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(upstream), **kw),
    )
    headers = {
        "Authorization": "Bearer chatgpt-user-token",
        "ChatGPT-Account-ID": "acct-1",
        "OpenAI-Beta": "responses_websockets=2026-02-06",
    }
    with h.client.websocket_connect(url, headers=headers) as ws:
        ws.send_text(json.dumps({**_create("hi"), "model": "gpt-5-codex"}))
        frames = _turn(ws)
    assert frames == events
    (request,) = seen
    assert str(request.url) == "https://chatgpt.com/backend-api/codex/responses"
    assert request.headers["authorization"] == "Bearer chatgpt-user-token"
    assert request.headers["chatgpt-account-id"] == "acct-1"
    assert "openai-beta" not in request.headers, "the WebSocket beta header stays here"
    assert not [k for k in request.headers if k.startswith("sec-websocket")]
    body = json.loads(request.content)
    assert body["model"] == "gpt-5-codex" and "type" not in body
    assert _engine_responses(h) == [], "never answered by Splash"


def test_codex_only_serves_the_routed_endpoints(harness_factory):
    """A routed request is answered by the local proxy, in Splash's error shape."""
    harness = harness_factory(installed=())
    _allow(harness, MODEL)
    _connected(harness)
    key = {"Authorization": f"Bearer {harness.state.secrets.get(SecretName.API_KEY)}"}
    for allowed in ("responses", "chat/completions", "completions"):
        response = harness.client.post(
            f"/api/codex/v1/{allowed}", json={"model": MODEL}, headers=key
        )
        body = response.json()
        assert "error" in body, f"{allowed} must be answered locally, got {response.text}"
        assert "code" in body["error"], body
    # An endpoint outside the routed set is refused rather than passed on.
    other = harness.client.post("/api/codex/v1/embeddings", json={"model": MODEL}, headers=key)
    assert other.status_code == 404


def test_codex_router_needs_a_credential_for_splash_models(harness_factory):
    """D58: the router used to serve Splash models to any local process."""
    harness = harness_factory(installed=())
    _allow(harness, MODEL)
    _connected(harness)
    secrets = harness.state.secrets
    for headers in ({}, {"Authorization": "Bearer sk-openai-user"}, {"x-api-key": "wrong"}):
        refused = harness.client.post(
            "/api/codex/v1/responses", json={"model": MODEL}, headers=headers
        )
        assert refused.status_code == 401, headers
        assert refused.json()["error"]["type"] == "authentication_error"
    # The router token in the path (what Connect writes).
    token = secrets.generate(SecretName.CODEX_ROUTER, prefix="")
    ok = harness.client.post(f"/api/codex/t/{token}/v1/responses", json={"model": MODEL})
    assert "error" not in ok.json() or ok.json()["error"]["code"] != "invalid_router_token"
    assert ok.status_code != 401
    wrong = harness.client.post("/api/codex/t/not-the-token/v1/responses", json={"model": MODEL})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "invalid_router_token"
    # Revoked: the same URL stops working at once.
    secrets.delete(SecretName.CODEX_ROUTER)
    gone = harness.client.post(f"/api/codex/t/{token}/v1/responses", json={"model": MODEL})
    assert gone.status_code == 401


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
    token = service.state.secrets.get(SecretName.CODEX_ROUTER)
    assert token and len(token) >= 43
    assert config["openai_base_url"] == f"http://127.0.0.1:8000/api/codex/t/{token}/v1"
    assert "model_provider" not in config and "profile" not in config and "model" not in config
    assert {"none", "max"} <= set(config["desktop"]["enabled-reasoning-efforts"])


async def test_codex_catalog_entry_has_the_fields_codex_requires(service, monkeypatch):
    native = [
        {"slug": "gpt-hidden", "visibility": "hide", "priority": 0, "base_instructions": "H"},
        {"slug": "gpt-b", "visibility": "list", "priority": 5, "base_instructions": "B"},
        {"slug": "gpt-a", "visibility": "list", "priority": 2, "base_instructions": "A"},
    ]
    monkeypatch.setattr(service, "native_codex_models", lambda codex_home: native)
    await service.connect("codex-app", confirm=False)
    entry = json.loads((service.state.paths.codex_app_dir / "models.json").read_text())["models"][0]
    assert {k: entry[k] for k in ("visibility", "priority", "support_verbosity")} == {
        "visibility": "list",
        "priority": 0,
        "support_verbosity": False,
    }
    assert entry["experimental_supported_tools"] == []
    assert entry["base_instructions"] == "A"
    # Q37/D64: Codex defers MCP and connector tools behind its tool search.
    assert entry["supports_search_tool"] is True


CHATGPT_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")


@pytest.mark.skipif(not CHATGPT_CODEX.exists(), reason="no ChatGPT app with Codex")
async def test_codex_app_parses_the_generated_catalog(service, tmp_path):
    """The installed app's own Codex accepts the catalog (Q17 found 0.160 refusing it)."""
    await service.connect("codex-app", confirm=False)
    home = tmp_path / "codex-home"
    home.mkdir()
    catalog = service.state.paths.codex_app_dir / "models.json"
    (home / "config.toml").write_text(f'model_catalog_json = "{catalog}"\n')
    result = subprocess.run(
        [str(CHATGPT_CODEX), "debug", "models"],
        env={"CODEX_HOME": str(home), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    models = json.loads(result.stdout)["models"]
    assert models[0]["slug"] == "mlx-community/Qwen3.6-35B-A3B-4bit"
    assert models[0]["supports_search_tool"] is True  # Q37/D64


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


def test_cli_install_links_match_splash_clients(service):
    """SPEC §10.7: each CLI row's Install link is the URL in splash/install/clients.py."""
    reference = Path(__file__).resolve().parents[2] / "splash" / "install" / "clients.py"
    if not reference.is_file():
        pytest.skip("the splash/ reference clone is not checked out")
    tree = ast.parse(reference.read_text(encoding="utf-8"))
    install_urls = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "INSTALL_URLS" for t in node.targets)
    )
    rows = {row.name: row for row in service.listing().cli}
    for name, url in install_urls.items():
        assert rows[name].install_url == url, name


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


# The ChatGPT app's own names that Splash 1.3.0 refuses (api_shapes.py `_namespace_alias`:
# each part of a namespace tool must match [A-Za-z0-9_-]{1,64}): a connector tool name of
# 70 characters (the owner's Tableau connector, ~/.codex/cache/codex_apps_tools), and
# names with characters outside the set.
TABLEAU = "mcp__codex_apps__tableau__mcp_only__eol_soon"
LONG = "tableau_mcp_only_eol_soon_list_pulse_metrics_from_metric_definition_id"
LONG_ALIAS = "tableau_mcp_only_eol_soon_list_pulse_metrics_from_met_b74a0d97d5"
PLUGIN = "browser@openai-bundled"
PLUGIN_ALIAS = "browser_openai-bundled_44e00c81aa"
DOTTED = "sites.add_custom_domain"
DOTTED_ALIAS = "sites_add_custom_domain_5a78e051fc"
OBJECT = {"type": "object", "properties": {}}
APP_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "name": "exec_command", "parameters": OBJECT},
    {"type": "function", "name": DOTTED, "parameters": OBJECT},
    {
        "type": "namespace",
        "name": "mcp__node_repl",
        "description": "",
        "tools": [{"type": "function", "name": "js", "parameters": OBJECT}],
    },
    {
        "type": "namespace",
        "name": TABLEAU,
        "description": "",
        "tools": [{"type": "function", "name": LONG, "parameters": OBJECT}],
    },
    {
        "type": "namespace",
        "name": PLUGIN,
        "description": "",
        "tools": [{"type": "function", "name": "open", "parameters": OBJECT}],
    },
    {"type": "web_search", "external_web_access": False},
]
ALIASED_TOOLS = [
    APP_TOOLS[0],
    {**APP_TOOLS[1], "name": DOTTED_ALIAS},
    APP_TOOLS[2],
    {**APP_TOOLS[3], "tools": [{**APP_TOOLS[3]["tools"][0], "name": LONG_ALIAS}]},
    {**APP_TOOLS[4], "name": PLUGIN_ALIAS},
]
CALL_LONG = {"type": "function", "namespace": TABLEAU, "name": LONG}


def _http(url: str) -> str:
    return url.replace(WS, "")


def _sse_frames(text: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[6:])
        for line in text.splitlines()
        if line.startswith("data: ") and line[6:].strip() not in ("", "[DONE]")
    ]


def _calls(frames: list[dict[str, Any]]) -> list[tuple[str, str | None, str]]:
    """Every function_call item the frames carry, as (kind, namespace, name)."""
    found = []
    for frame in frames:
        items = [frame.get("item"), *((frame.get("response") or {}).get("output") or [])]
        for item in items:
            if isinstance(item, dict) and item.get("type") == "function_call":
                found.append((frame["type"], item.get("namespace"), item["name"]))
    return found


def test_the_fake_engine_refuses_the_app_names_as_splash_does(harness_factory):
    """Without the router's aliases the engine answers what the owner saw."""
    h, _ = _ws_ready(harness_factory)
    key = {"Authorization": f"Bearer {h.state.secrets.get(SecretName.API_KEY)}"}
    body = {"model": MODEL, "input": "hi", "tools": APP_TOOLS[2:5]}
    refused = h.client.post("/v1/responses", json=body, headers=key)
    assert refused.status_code == 400
    assert refused.json()["error"]["message"] == "invalid namespace tool name"


def test_codex_http_aliases_tool_names_splash_refuses(harness_factory):
    """D63: for a Splash model every refused name gets a valid alias on the way in and
    its original back on the way out; valid names are left alone."""
    h, url = _ws_ready(harness_factory)
    body = _create("go", tools=APP_TOOLS, tool_choice=CALL_LONG)
    del body["type"]
    response = h.client.post(_http(url), json=body)
    assert response.status_code == 200, response.text
    (sent,) = _engine_responses(h)
    assert sent["tools"] == ALIASED_TOOLS
    assert sent["tool_choice"] == {"type": "function", "namespace": TABLEAU, "name": LONG_ALIAS}
    frames = _sse_frames(response.text)
    assert _calls(frames) == [
        ("response.output_item.added", TABLEAU, LONG),
        ("response.output_item.done", TABLEAU, LONG),
        ("response.completed", TABLEAU, LONG),
    ]
    (done,) = [f for f in frames if f["type"] == "response.function_call_arguments.done"]
    assert done["name"] == LONG
    assert LONG_ALIAS not in response.text

    # The next turn sends the call back with the names Codex knows.
    call = next(
        f["item"]
        for f in frames
        if f["type"] == "response.output_item.done" and f["item"]["type"] == "function_call"
    )
    output = {"type": "function_call_output", "call_id": call["call_id"], "output": "42"}
    body["input"] = [*body["input"], call, output]
    body["tool_choice"] = "auto"
    again = h.client.post(_http(url), json=body)
    assert again.status_code == 200, again.text
    second = _engine_responses(h)[1]
    assert second["input"][1] == {**call, "name": LONG_ALIAS}
    assert second["input"][2] == output
    assert _text(_sse_frames(again.text)) == "Hello from the fake engine."


def test_codex_http_aliases_a_plain_function_and_restores_a_json_answer(harness_factory):
    h, url = _ws_ready(harness_factory)
    forced = {"type": "function", "name": DOTTED}
    body = {
        "model": MODEL,
        "input": "go",
        "tools": APP_TOOLS,
        "tool_choice": forced,
        "stream": False,
    }
    response = h.client.post(_http(url), json=body)
    assert response.status_code == 200, response.text
    assert _engine_responses(h)[0]["tool_choice"] == {"type": "function", "name": DOTTED_ALIAS}
    (call,) = [i for i in response.json()["output"] if i["type"] == "function_call"]
    assert call["name"] == DOTTED and "namespace" not in call
    assert DOTTED_ALIAS not in response.text


def test_codex_router_leaves_valid_names_alone(harness_factory):
    h, url = _ws_ready(harness_factory)
    valid = APP_TOOLS[:1] + APP_TOOLS[2:3]
    forced = {"type": "function", "namespace": "mcp__node_repl", "name": "js"}
    body = _create("go", tools=valid, tool_choice=forced)
    del body["type"]
    response = h.client.post(_http(url), json=body)
    assert response.status_code == 200, response.text
    (sent,) = _engine_responses(h)
    assert sent["tools"] == valid and sent["tool_choice"] == forced
    assert ("response.output_item.done", "mcp__node_repl", "js") in _calls(
        _sse_frames(response.text)
    )


def test_codex_websocket_aliases_tool_names_across_turns(harness_factory):
    """A call made under an alias continues on the same socket with only new items."""
    h, url = _ws_ready(harness_factory)
    with h.client.websocket_connect(url) as ws:
        ws.send_text(json.dumps(_create("go", tools=APP_TOOLS, tool_choice=CALL_LONG)))
        first = _turn(ws)
        assert first[-1]["type"] == "response.completed", first[-1]
        assert _calls(first) == [
            ("response.output_item.added", TABLEAU, LONG),
            ("response.output_item.done", TABLEAU, LONG),
            ("response.completed", TABLEAU, LONG),
        ]
        assert LONG_ALIAS not in json.dumps(first)
        call = next(
            f["item"]
            for f in first
            if f["type"] == "response.output_item.done" and f["item"]["type"] == "function_call"
        )
        output = {"type": "function_call_output", "call_id": call["call_id"], "output": "42"}
        ws.send_text(
            json.dumps(
                _create(
                    "",
                    tools=APP_TOOLS,
                    previous_response_id=first[-1]["response"]["id"],
                    input=[output],
                )
            )
        )
        second = _turn(ws)
        assert second[-1]["type"] == "response.completed", second[-1]
    engine = _engine_responses(h)
    assert engine[0]["tools"] == ALIASED_TOOLS
    assert engine[1]["tools"] == ALIASED_TOOLS
    calls = [i for i in engine[1]["input"] if i["type"] == "function_call"]
    assert calls == [{**call, "name": LONG_ALIAS}]
    assert engine[1]["input"][-1] == output


def test_codex_router_never_renames_tools_for_native_models(harness_factory, monkeypatch):
    h, url = _ws_ready(harness_factory)
    seen: list[httpx.Request] = []
    call = {**CALL_LONG, "type": "function_call", "call_id": "c1", "arguments": "{}"}
    events = [
        {"type": "response.created", "response": {"id": "resp_up"}},
        {"type": "response.output_item.done", "item": call},
        {"type": "response.completed", "response": {"id": "resp_up", "output": [call]}},
    ]

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        text = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(text.encode()),
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(upstream), **kw)
    )
    body = {**_create("go", tools=APP_TOOLS, tool_choice=CALL_LONG), "model": "gpt-5-codex"}
    with h.client.websocket_connect(url, headers={"ChatGPT-Account-ID": "acct-1"}) as ws:
        ws.send_text(json.dumps(body))
        frames = _turn(ws)
    assert frames == events
    sent = json.loads(seen[0].content)
    assert sent["tools"] == APP_TOOLS and sent["tool_choice"] == CALL_LONG


# --- Codex router: on-demand tool search for Splash models (Q37, D64) ----------------

# What Codex 0.162 sends when the catalog entry has `supports_search_tool`
# (codex-rs core/src/tools/handlers/tool_search_spec.rs): one client-run search tool in
# place of every MCP and connector tool.
SEARCH_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query for deferred tools."},
        "limit": {
            "type": "number",
            "description": "Maximum number of tools to return. Defaults to 8.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}
SEARCH_TOOL = {
    "type": "tool_search",
    "execution": "client",
    "description": "# Tool discovery\n\nSearches over deferred tool metadata with BM25.",
    "parameters": SEARCH_PARAMETERS,
}
SEARCH_TOOLS = [APP_TOOLS[0], SEARCH_TOOL, APP_TOOLS[-1]]
SEARCH_FUNCTION = {
    "type": "function",
    "name": "tool_search",
    "description": SEARCH_TOOL["description"],
    "parameters": SEARCH_PARAMETERS,
}
# A tool_search_output as codex-rs builds it (core/src/tools/context.rs
# `ToolSearchOutput::to_response_item`): found tools as namespaces, `defer_loading` set.
FOUND: list[dict[str, Any]] = [
    {
        "type": "namespace",
        "name": TABLEAU,
        "description": "Tableau Pulse metrics.",
        "tools": [
            {
                "type": "function",
                "name": LONG,
                "description": "List Pulse metrics for a metric definition.",
                "strict": False,
                "defer_loading": True,
                "parameters": OBJECT,
            }
        ],
    },
    {
        "type": "namespace",
        "name": "mcp__node_repl",
        "description": "Node REPL.",
        "tools": [
            {
                "type": "function",
                "name": "js",
                "description": "Run JavaScript in a persistent Node REPL.",
                "defer_loading": True,
                "parameters": OBJECT,
            }
        ],
    },
]


def _connector_tools(count: int) -> list[dict[str, Any]]:
    """`count` connector tools inline, as Codex sends them without tool search."""
    schema = {
        "type": "object",
        "properties": {
            f"field_{i}": {"type": "string", "description": "x" * 120} for i in range(8)
        },
    }
    return [
        {
            "type": "namespace",
            "name": f"mcp__codex_apps__connector_{n // 30}",
            "description": "A ChatGPT Apps connector.",
            "tools": [
                {
                    "type": "function",
                    "name": f"action_{n}",
                    "description": "Does one thing in a connected app. " * 10,
                    "parameters": schema,
                }
            ],
        }
        for n in range(count)
    ]


def _search_call(frames: list[dict[str, Any]]) -> dict[str, Any]:
    item: dict[str, Any]
    (item,) = [
        f["item"]
        for f in frames
        if f["type"] == "response.output_item.done" and f["item"]["type"] == "tool_search_call"
    ]
    return item


def test_codex_tool_search_keeps_the_engine_request_small(harness_factory):
    """Inline, 600 connector tools are what overflowed the owner's context; behind
    tool_search the engine sees one function, and after a search only what was found."""
    h, url = _ws_ready(harness_factory)
    inline = _create("hi", tools=[APP_TOOLS[0], *_connector_tools(600)])
    del inline["type"]
    assert h.client.post(_http(url), json=inline).status_code == 200
    deferred = _create("hi", tools=SEARCH_TOOLS)
    del deferred["type"]
    assert h.client.post(_http(url), json=deferred).status_code == 200
    before, after = (len(json.dumps(b).encode()) for b in _engine_responses(h))
    assert before > 900_000
    assert after < 2_000
    sent = _engine_responses(h)[1]
    assert sent["tools"] == [APP_TOOLS[0], SEARCH_FUNCTION]


def test_codex_tool_search_call_reaches_codex_as_a_tool_search_call(harness_factory):
    h, url = _ws_ready(harness_factory)
    body = _create("find the node repl with tool_search", tools=SEARCH_TOOLS)
    del body["type"]
    response = h.client.post(_http(url), json=body)
    assert response.status_code == 200, response.text
    (sent,) = _engine_responses(h)
    assert sent["tools"] == [APP_TOOLS[0], SEARCH_FUNCTION]
    frames = _sse_frames(response.text)
    call = _search_call(frames)
    assert call["execution"] == "client"
    assert call["arguments"] == {"query": "find the node repl with tool_search"}
    assert call["call_id"].startswith("call_")
    added = [f["item"]["type"] for f in frames if f["type"] == "response.output_item.added"]
    completed = [i["type"] for i in frames[-1]["response"]["output"]]
    assert [k for k in added if k != "reasoning"] == ["tool_search_call"]
    assert [k for k in completed if k != "reasoning"] == ["tool_search_call"]
    assert _calls(frames) == []

    # Non-streamed answers get the same item.
    response = h.client.post(_http(url), json={**body, "stream": False})
    (item,) = [i for i in response.json()["output"] if i["type"] != "reasoning"]
    assert (item["type"], item["execution"]) == ("tool_search_call", "client")
    assert item["arguments"] == {"query": "find the node repl with tool_search"}


def test_codex_found_tools_are_callable_on_the_next_turn(harness_factory):
    """The tool_search_output's tools reach Splash as tools (a 70-character name
    aliased), the search items as function items, and the call comes back under the
    names Codex knows."""
    h, url = _ws_ready(harness_factory)
    body = _create("find the tableau metrics tool with tool_search", tools=SEARCH_TOOLS)
    del body["type"]
    first = h.client.post(_http(url), json=body)
    call = _search_call(_sse_frames(first.text))
    output = {
        "type": "tool_search_output",
        "call_id": call["call_id"],
        "status": "completed",
        "execution": "client",
        "tools": FOUND,
    }
    body["input"] = [*body["input"], call, output]
    body["tool_choice"] = CALL_LONG
    second = h.client.post(_http(url), json=body)
    assert second.status_code == 200, second.text
    sent = _engine_responses(h)[1]
    assert sent["tools"] == [
        APP_TOOLS[0],
        SEARCH_FUNCTION,
        {
            "type": "namespace",
            "name": TABLEAU,
            "description": "Tableau Pulse metrics.",
            "tools": [
                {
                    "type": "function",
                    "name": LONG_ALIAS,
                    "description": "List Pulse metrics for a metric definition.",
                    "strict": False,
                    "parameters": OBJECT,
                }
            ],
        },
        {
            "type": "namespace",
            "name": "mcp__node_repl",
            "description": "Node REPL.",
            "tools": [
                {
                    "type": "function",
                    "name": "js",
                    "description": "Run JavaScript in a persistent Node REPL.",
                    "parameters": OBJECT,
                }
            ],
        },
    ]
    assert sent["input"][1] == {
        "type": "function_call",
        "call_id": call["call_id"],
        "name": "tool_search",
        "arguments": '{"query":"find the tableau metrics tool with…"}',
    }
    assert call["arguments"] == {"query": "find the tableau metrics tool with…"}
    assert sent["input"][2] == {
        "type": "function_call_output",
        "call_id": call["call_id"],
        "output": "These tools are now available; call them directly:\n"
        "- mcp__codex_apps__tab__tableau_mcp_only_eol_soo__c8220a4d8a99177b: "
        "List Pulse metrics for a metric definition.\n"
        "- mcp__node_repl__js: Run JavaScript in a persistent Node REPL.",
    }
    frames = _sse_frames(second.text)
    assert ("response.output_item.done", TABLEAU, LONG) in _calls(frames)
    assert LONG_ALIAS not in second.text

    # The call and its output go back; the found tools stay loaded from history.
    found_call = next(
        f["item"]
        for f in frames
        if f["type"] == "response.output_item.done" and f["item"]["type"] == "function_call"
    )
    result = {"type": "function_call_output", "call_id": found_call["call_id"], "output": "3"}
    body["input"] = [*body["input"], found_call, result]
    body["tool_choice"] = "auto"
    third = h.client.post(_http(url), json=body)
    assert third.status_code == 200, third.text
    sent = _engine_responses(h)[2]
    assert sent["input"][3] == {**found_call, "name": LONG_ALIAS}
    assert sent["input"][4] == result
    assert [t["name"] for t in sent["tools"]] == [
        "exec_command",
        "tool_search",
        TABLEAU,
        "mcp__node_repl",
    ]


def test_codex_tool_search_merges_repeated_finds(harness_factory):
    """Two searches that find tools in one namespace give one namespace, once each."""
    h, url = _ws_ready(harness_factory)
    more = {
        "type": "namespace",
        "name": "mcp__node_repl",
        "description": "Node REPL.",
        "tools": [
            {**FOUND[1]["tools"][0]},
            {"type": "function", "name": "js_reset", "parameters": OBJECT, "defer_loading": True},
            {"type": "custom", "name": "js_freeform", "format": {"type": "text"}},
        ],
    }
    calls = [
        {"type": "tool_search_call", "call_id": f"s{n}", "execution": "client", "arguments": {}}
        for n in (1, 2)
    ]
    outputs = [
        {"type": "tool_search_output", "call_id": "s1", "status": "completed",
         "execution": "client", "tools": [FOUND[1]]},
        {"type": "tool_search_output", "call_id": "s2", "status": "completed",
         "execution": "client", "tools": [more]},
    ]  # fmt: skip
    body = _create("go", tools=SEARCH_TOOLS)
    del body["type"]
    body["input"] = [*body["input"], calls[0], outputs[0], calls[1], outputs[1]]
    assert h.client.post(_http(url), json=body).status_code == 200
    (sent,) = _engine_responses(h)
    (repl,) = [t for t in sent["tools"] if t.get("name") == "mcp__node_repl"]
    assert [c["name"] for c in repl["tools"]] == ["js", "js_reset"]
    assert [i["type"] for i in sent["input"]] == [
        "message",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
    ]
    assert sent["input"][1]["arguments"] == "{}"


def test_codex_websocket_tool_search_across_turns(harness_factory):
    """Search, then call what was found, on one socket with only new items."""
    h, url = _ws_ready(harness_factory)
    with h.client.websocket_connect(url) as ws:
        ws.send_text(json.dumps(_create("use tool_search for the repl", tools=SEARCH_TOOLS)))
        first = _turn(ws)
        assert first[-1]["type"] == "response.completed", first[-1]
        call = _search_call(first)
        output = {
            "type": "tool_search_output",
            "call_id": call["call_id"],
            "status": "completed",
            "execution": "client",
            "tools": FOUND,
        }
        forced = {"type": "function", "namespace": "mcp__node_repl", "name": "js"}
        ws.send_text(
            json.dumps(
                _create(
                    "",
                    tools=SEARCH_TOOLS,
                    tool_choice=forced,
                    previous_response_id=first[-1]["response"]["id"],
                    input=[output],
                )
            )
        )
        second = _turn(ws)
        assert second[-1]["type"] == "response.completed", second[-1]
        assert ("response.output_item.done", "mcp__node_repl", "js") in _calls(second)
    engine = _engine_responses(h)
    assert [i["type"] for i in engine[1]["input"] if i["type"] != "reasoning"] == [
        "message",
        "function_call",
        "function_call_output",
    ]
    (search,) = [i for i in engine[1]["input"] if i["type"] == "function_call"]
    assert search["name"] == "tool_search"
    assert [t["name"] for t in engine[1]["tools"]] == [
        "exec_command",
        "tool_search",
        TABLEAU,
        "mcp__node_repl",
    ]


def test_codex_router_leaves_tool_search_alone_for_native_models(harness_factory, monkeypatch):
    h, url = _ws_ready(harness_factory)
    seen: list[httpx.Request] = []
    call = {
        "type": "tool_search_call",
        "call_id": "s1",
        "execution": "client",
        "arguments": {"query": "repl"},
    }
    events = [
        {"type": "response.created", "response": {"id": "resp_up"}},
        {"type": "response.output_item.done", "item": call},
        {"type": "response.completed", "response": {"id": "resp_up", "output": [call]}},
    ]

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        text = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(text.encode()),
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(upstream), **kw)
    )
    output = {
        "type": "tool_search_output",
        "call_id": "s0",
        "status": "completed",
        "execution": "client",
        "tools": FOUND,
    }
    body = {**_create("go", tools=SEARCH_TOOLS), "model": "gpt-5-codex"}
    body["input"] = [*body["input"], {**call, "call_id": "s0"}, output]
    with h.client.websocket_connect(url, headers={"ChatGPT-Account-ID": "acct-1"}) as ws:
        ws.send_text(json.dumps(body))
        frames = _turn(ws)
    assert frames == events
    sent = json.loads(seen[0].content)
    assert sent["tools"] == SEARCH_TOOLS and sent["input"] == body["input"]

"""The Claude gateway while the engine is stopped (SPEC §11.3.1, "When the engine is stopped
while connected"): it loads the mapped model, and when that fails it answers with an
Anthropic-shaped overloaded_error that says no model is loaded, not the load's own error."""

from __future__ import annotations

import socket
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from splash_gui.integrations.service import IntegrationsService

from .fakeengine import MODEL, EngineHarness

OVERLOADED = {
    "type": "error",
    "error": {"type": "overloaded_error", "message": "Splashboard: no model loaded"},
}
BODY = {"model": "claude-opus-5", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """A throwaway home with the desktop apps 'installed' (the service's `home`)."""
    home = tmp_path / "home"
    for name in ("Claude.app", "Codex.app", "ChatGPT.app"):
        (home / "Applications" / name / "Contents" / "MacOS").mkdir(parents=True)
    return home


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _connected(
    harness_factory: Callable[..., EngineHarness],
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    slots: dict[str, str | None],
) -> tuple[EngineHarness, str]:
    h = harness_factory()
    port = _free_port()
    h.patch_settings(
        {"global": {"integrations": {"claude_desktop": {"port": port, "slots": slots}}}}
    )
    service = IntegrationsService(h.state, home=fake_home)
    monkeypatch.setattr(h.state, "integrations", service)
    monkeypatch.setattr(service, "running", lambda name: False)
    monkeypatch.setattr(
        h.state.macos, "open", lambda *args: subprocess.CompletedProcess(args, 0, "", "")
    )
    reply = h.client.post("/api/admin/integrations/claude-desktop/connect", json={})
    assert reply.status_code == 200, reply.text
    return h, f"http://127.0.0.1:{port}"


def _slots(target: str | None) -> dict[str, str | None]:
    return {
        "claude-fable-5": None,
        "claude-opus-5": target,
        "claude-sonnet-5": None,
        "claude-haiku-4-5-20251001": None,
    }


def test_a_stopped_engine_loads_the_mapped_model_for_the_request(
    harness_factory: Callable[..., EngineHarness],
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, base = _connected(harness_factory, fake_home, monkeypatch, _slots(MODEL))
    try:
        assert h.engine()["state"] == "stopped"
        reply = httpx.post(base + "/v1/messages", json=BODY, timeout=60)
        assert reply.status_code == 200, reply.text
        assert h.engine()["model"] == MODEL
    finally:
        h.client.post("/api/admin/integrations/claude-desktop/disconnect")


def test_a_mapped_model_that_is_not_installed_answers_no_model_loaded(
    harness_factory: Callable[..., EngineHarness],
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, base = _connected(harness_factory, fake_home, monkeypatch, _slots("not/installed-model"))
    try:
        reply = httpx.post(base + "/v1/messages", json=BODY, timeout=30)
        assert reply.status_code == 503
        assert reply.json() == OVERLOADED
        assert h.engine()["state"] == "stopped"
    finally:
        h.client.post("/api/admin/integrations/claude-desktop/disconnect")


def test_a_failed_auto_load_answers_no_model_loaded(
    harness_factory: Callable[..., EngineHarness],
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, base = _connected(harness_factory, fake_home, monkeypatch, _slots(MODEL))
    try:
        h.patch_settings({"global": {"routing": {"auto_load": False}}})
        reply = httpx.post(base + "/v1/messages", json=BODY, timeout=30)
        assert reply.status_code == 503
        assert reply.json() == OVERLOADED
        assert h.engine()["state"] == "stopped"
    finally:
        h.client.post("/api/admin/integrations/claude-desktop/disconnect")

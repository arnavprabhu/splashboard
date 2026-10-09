"""`splash launch claude-desktop|codex-app` and `--restore` through the CLI.
The app boundary is faked (whether the app runs, quitting it, the
gateway, the Codex model list and every `open`), so the configuration files written and
restored are the real ones, under a throwaway home."""

from __future__ import annotations

import builtins
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from splash_gui import app as app_module
from splash_gui import cli as cli_module
from splash_gui.integrations.service import IntegrationsService
from splash_gui.system.macos import RecordingMacOS

from .fakeengine import EngineHarness

# The CLI passes per-request timeouts, which Starlette's TestClient ignores.
pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")


class _Shared:
    """The harness client, minus close(): main() closes its client on the way out."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def close(self) -> None:
        return None


class _Terminal:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


@pytest.fixture
def h(
    harness_factory: Callable[..., EngineHarness], monkeypatch: pytest.MonkeyPatch
) -> EngineHarness:
    harness = harness_factory()
    real = cli_module.Client

    class Client(real):  # type: ignore[misc, valid-type]
        def __init__(self, port: int | None = None) -> None:
            super().__init__(port)
            self.http.close()
            self.http: Any = _Shared(harness.client)

    monkeypatch.setattr(cli_module, "Client", Client)
    monkeypatch.delenv("SPLASH_PORT", raising=False)
    return harness


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Both apps 'installed'. HOME points here too: `--restore` without the manager builds
    its service with the user's home, which must never be the developer's real one."""
    home = tmp_path / "home"
    for name in ("Claude.app", "Codex.app"):
        (home / "Applications" / name / "Contents" / "MacOS").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def desktop(h: EngineHarness, fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The integrations service with the app boundary faked. The flag says whether the
    app is running; quitting it clears the flag and reports that it had been running."""
    flag = {"up": False}

    def running(self: IntegrationsService, name: str) -> bool:
        return flag["up"]

    async def quit_app(self: IntegrationsService, name: str) -> bool:
        was_running = flag["up"]
        flag["up"] = False
        return was_running

    async def no_gateway(self: IntegrationsService) -> None:
        return None

    monkeypatch.setattr(IntegrationsService, "running", running)
    monkeypatch.setattr(IntegrationsService, "quit_app", quit_app)
    monkeypatch.setattr(IntegrationsService, "start_gateway", no_gateway)
    monkeypatch.setattr(IntegrationsService, "native_codex_models", lambda self, codex_home: [])
    macos = RecordingMacOS()
    monkeypatch.setattr(h.state, "macos", macos)
    service = IntegrationsService(h.state, home=fake_home)
    monkeypatch.setattr(h.state, "integrations", service)
    return {"flag": flag, "macos": macos, "service": service, "state": h.state}


def on_terminal(monkeypatch: pytest.MonkeyPatch, answer: str | None) -> list[str]:
    """A TTY stdin answering `answer`; `None` means no question may be asked."""
    asked: list[str] = []

    def fake_input(prompt: str = "") -> str:
        asked.append(prompt)
        if answer is None:
            raise AssertionError(f"the CLI asked a question it should not ask: {prompt}")
        return answer

    monkeypatch.setattr(sys, "stdin", _Terminal(True))
    monkeypatch.setattr(builtins, "input", fake_input)
    return asked


def opened(desktop: dict[str, Any]) -> list[list[str]]:
    return [call for call in desktop["macos"].calls if call[:2] == ["/usr/bin/open", "-a"]]


def records(desktop: dict[str, Any]) -> dict[str, Any]:
    return dict(desktop["service"].records)


def test_connecting_a_stopped_app_asks_nothing(
    h: EngineHarness, desktop: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = on_terminal(monkeypatch, None)
    assert cli_module.main(["launch", "claude-desktop"]) == 0
    assert asked == []
    assert "claude-desktop" in records(desktop)
    assert len(opened(desktop)) == 1


def test_a_running_app_restarts_only_after_a_yes(
    h: EngineHarness,
    desktop: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    desktop["flag"]["up"] = True
    asked = on_terminal(monkeypatch, "y")
    assert cli_module.main(["launch", "claude-desktop"]) == 0, capsys.readouterr().err
    assert len(asked) == 1
    assert asked[0].startswith("Claude will restart. Your previous configuration is backed up")
    assert asked[0].endswith("Continue? [y/N] ")
    assert "claude-desktop" in records(desktop)
    assert desktop["flag"]["up"] is False, "the app was quit before it was opened again"


def test_a_running_app_is_left_alone_on_no(
    h: EngineHarness,
    desktop: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    desktop["flag"]["up"] = True
    asked = on_terminal(monkeypatch, "")
    code = cli_module.main(["launch", "codex-app"])
    _, err = capsys.readouterr()
    assert code == 1 and "Not connected." in err
    assert asked and asked[0].startswith("Codex will restart.")
    assert records(desktop) == {}
    assert desktop["flag"]["up"] is True
    assert opened(desktop) == []


def test_off_a_terminal_a_running_app_is_not_restarted(
    h: EngineHarness,
    desktop: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    desktop["flag"]["up"] = True
    monkeypatch.setattr(sys, "stdin", _Terminal(False))
    code = cli_module.main(["launch", "claude-desktop"])
    _, err = capsys.readouterr()
    assert code == 2, "usage error: the question needs a terminal"
    assert "Claude is running, and connecting restarts it." in err
    assert records(desktop) == {}
    assert desktop["flag"]["up"] is True
    assert opened(desktop) == []


def test_restore_through_a_running_manager_disconnects(
    h: EngineHarness, desktop: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    on_terminal(monkeypatch, None)
    assert cli_module.main(["launch", "claude-desktop"]) == 0
    assert "claude-desktop" in records(desktop)
    assert cli_module.main(["launch", "claude-desktop", "--restore"]) == 0
    assert records(desktop) == {}


@pytest.mark.parametrize("name", ["claude-desktop", "codex-app"])
@pytest.mark.parametrize("running", [True, False])
def test_restore_with_the_manager_down_reopens_only_a_running_app(
    h: EngineHarness,
    desktop: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    running: bool,
) -> None:
    on_terminal(monkeypatch, None)
    assert cli_module.main(["launch", name]) == 0
    assert name in records(desktop)
    desktop["flag"]["up"] = running
    desktop["macos"].calls.clear()
    monkeypatch.setattr(cli_module.Client, "running", lambda self: False)
    # `--restore` without the manager builds its own state from the paths: the test's.
    monkeypatch.setattr(app_module, "build_state", lambda config: desktop["state"])
    assert cli_module.main(["launch", name, "--restore"]) == 0
    state_file = json.loads(desktop["state"].paths.integrations_state.read_text())
    assert state_file == {}, "the record is gone once everything is restored"
    assert bool(opened(desktop)) is running, "reopened only when it was running"
    assert desktop["flag"]["up"] is False

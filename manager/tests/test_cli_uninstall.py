"""`splash doctor --uninstall` (SPEC §19, PKG-12) against the fake-engine manager. `launchctl` is
never run here: the agent lookup and the bootout are stubbed."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from splash_gui import cli as cli_module

from .fakeengine import MODEL, EngineHarness
from .test_cli_commands import _Shared, run

pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")


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
def launchd(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """No real launchctl: agent_pid reports nothing loaded unless a test says otherwise."""
    calls: list[list[str]] = []
    monkeypatch.setattr(cli_module, "agent_pid", lambda label: None)

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(list(argv))
        raise AssertionError("no subprocess expected")

    monkeypatch.setattr("splash_gui.cli.subprocess.run", fake_run)
    return calls


def test_yes_removes_the_data_and_keeps_models(
    h: EngineHarness, capsys: pytest.CaptureFixture[str], launchd: list[list[str]]
) -> None:
    code, out, err = run(capsys, "doctor", "--uninstall", "--yes", "--json")
    assert code == 0, out
    result = json.loads(out)
    assert "Splashboard data in" in err  # the sizes go to stderr under --json
    assert result["shim_removed"] in (True, False) and result["freed_bytes"] > 0
    assert not any(path.endswith("/models") for path in result["deleted"])
    assert any(path.endswith("/models") for path in result["kept"])
    assert launchd == []


def test_the_sizes_and_steps_are_shown(
    h: EngineHarness, capsys: pytest.CaptureFixture[str], launchd: list[list[str]]
) -> None:
    code, out, _ = run(capsys, "doctor", "--uninstall", "--yes")
    assert code == 0
    assert "data (settings, chats, usage, logs)" in out and "models " in out and "cache " in out
    assert "· Restore Claude Desktop" in out
    assert "Drag Splashboard.app to the Trash" in out


def test_without_a_terminal_it_needs_yes(
    h: EngineHarness, capsys: pytest.CaptureFixture[str], launchd: list[list[str]]
) -> None:
    code, _, err = run(capsys, "doctor", "--uninstall")
    assert code == 2 and "--yes" in err
    assert h.client.get("/api/admin/models").json()["models"][0]["id"] == MODEL


def test_a_manager_run_by_its_agent_is_booted_out_not_stopped(
    h: EngineHarness,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[Any] = []
    real_request = cli_module.Client.request

    def request(self: Any, method: str, path: str, body: Any = None) -> Any:
        if path == "/uninstall":
            sent.append(body)
        return real_request(self, method, path, body)

    calls: list[list[str]] = []
    monkeypatch.setattr(cli_module.Client, "request", request)
    monkeypatch.setattr(cli_module, "manager_pid", lambda paths: (4242, 1.0))
    monkeypatch.setattr(cli_module, "agent_pid", lambda label: 4242)
    monkeypatch.setattr("splash_gui.cli.subprocess.run", lambda argv, **kw: calls.append(argv))

    code, _, _ = run(capsys, "doctor", "--uninstall", "--yes")

    assert code == 0
    assert sent and sent[0]["stop"] is False
    assert calls and calls[0][:2] == ["/bin/launchctl", "bootout"]
    assert calls[0][2].endswith("/io.github.arnavprabhu.splashboard.manager")


def test_another_installs_agent_is_never_touched(
    h: EngineHarness, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(cli_module, "manager_pid", lambda paths: (4242, 1.0))
    monkeypatch.setattr(cli_module, "agent_pid", lambda label: 999)  # the owner's real agent
    monkeypatch.setattr("splash_gui.cli.subprocess.run", lambda argv, **kw: calls.append(argv))

    assert run(capsys, "doctor", "--uninstall", "--yes")[0] == 0
    assert calls == []

"""`splash` commands against a manager (SPEC §12.2), with the CLI's HTTP client
pointed at the harness's in-process app."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from splash_gui import cli as cli_module
from splash_gui.usage.db import UsageDB

from .fakeengine import MODEL, EngineHarness

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
    return harness


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli_module.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_ls_load_status_unload(h: EngineHarness, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "ls", "--json")
    assert code == 0 and [m["id"] for m in json.loads(out)["models"]] == [MODEL]
    code, out, _ = run(capsys, "ls")
    assert code == 0 and MODEL in out and "ready" in out
    code, out, _ = run(capsys, "load", f"{MODEL}:no-think")
    assert code == 0 and out.strip() == f"{MODEL}:no-think"
    assert h.engine()["state"] in ("ready", "busy") and h.engine()["model"] == MODEL
    code, out, _ = run(capsys, "status")
    assert code == 0 and f"Engine:   ready  {MODEL}" in out
    assert "OpenAI:   http://127.0.0.1:8000/v1" in out
    code, out, _ = run(capsys, "ps")
    assert code == 0 and "Requests: 0 in flight" in out and "OpenAI" not in out
    code, out, _ = run(capsys, "status", "--json")
    assert json.loads(out)["model"] == MODEL
    assert run(capsys, "unload")[0] == 0
    assert h.engine()["state"] == "stopped"


def test_config_get_set_unset_validates(
    h: EngineHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "config", "set", "routing.load_timeout", "60")[0] == 0
    code, out, _ = run(capsys, "config", "get", "routing.load_timeout")
    assert code == 0 and float(out) == 60
    code, _, err = run(capsys, "config", "set", "serve.queue_size", "0")
    assert code == 1 and "splash:" in err
    assert h.settings_document()["global"]["serve"]["queue_size"] == 32
    assert run(capsys, "config", "unset", "routing.load_timeout")[0] == 0
    assert h.settings_document()["global"]["routing"]["load_timeout"] == 120


def test_rm_and_version(h: EngineHarness, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "version")
    assert code == 0 and "1.2.0" in out
    assert run(capsys, "rm", MODEL, "--yes")[0] == 0
    assert h.client.get("/api/admin/models").json()["models"] == []
    code, _, err = run(capsys, "rm", MODEL, "--yes")
    assert code == 1 and "not installed" in err.lower()


def test_launch_hands_the_profile_to_splash_s_configurator_and_records_it(
    h: EngineHarness, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, list[str], dict[str, str]]] = []
    monkeypatch.setattr(os, "execve", lambda path, argv, env: calls.append((path, argv, env)))
    monkeypatch.setattr(cli_module, "discover", lambda *a, **k: h.state.engine_cached())
    code, _, err = run(capsys, "launch", "claude", "--model", f"{MODEL}:no-think", "--", "-p", "hi")
    assert code == 0, err
    _, argv, env = calls[-1]
    assert argv[-1].endswith("helpers/launch_client.py")
    spec = json.loads(env["SPLASH_GUI_CLIENT_SPEC"])
    assert spec["client"] == "claude" and spec["model"] == f"{MODEL}:no-think"
    assert spec["args"] == ["-p", "hi"] and spec["print"] is False
    assert spec["url"] == "http://127.0.0.1:8000" and spec["context"] == 262144
    assert env["SPLASH_PORT"] == "8000" and "SPLASH_API_KEY" not in env
    db = UsageDB(h.state.paths.usage_db)
    try:
        assert "claude" in db.last_launches()
    finally:
        db.close()
    listing = h.client.get("/api/admin/integrations").json()["cli"]
    assert next(r for r in listing if r["name"] == "claude")["last_launched_at"]
    run(capsys, "launch", "codex", "--print")
    assert json.loads(calls[-1][2]["SPLASH_GUI_CLIENT_SPEC"])["print"] is True


def test_launch_keeps_pythonpath_and_sends_the_key_only_when_required(
    h: EngineHarness, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from splash_gui.secrets import SecretName, SecretStore, backend_from_env

    calls: list[dict[str, str]] = []
    monkeypatch.setattr(os, "execve", lambda path, argv, env: calls.append(env))
    monkeypatch.setattr(cli_module, "discover", lambda *a, **k: h.state.engine_cached())
    monkeypatch.setenv("PYTHONPATH", "/user/own/path")
    # A backend the CLI process reads back (memory would be a fresh store).
    monkeypatch.setenv("SPLASH_GUI_SECRETS", "file")
    SecretStore(backend_from_env(h.state.paths)).set(SecretName.API_KEY, "sk-splash-test-key-123")
    assert run(capsys, "launch", "claude", "--model", MODEL)[0] == 0
    env = calls[-1]
    assert env["PYTHONPATH"] == "/user/own/path", "the client keeps the user's PYTHONPATH"
    assert env["SPLASH_GUI_ENGINE_PKG"] == str(h.state.engine_cached().pkg)
    # Auth is off: the key is not handed to the configurators (Hermes would
    # write it into its profile's config.yaml).
    assert "SPLASH_API_KEY" not in env

    settings = h.state.paths.settings_file
    document = json.loads(settings.read_text()) if settings.exists() else {}
    document.setdefault("global", {}).setdefault("security", {})["api_key_required"] = True
    settings.write_text(json.dumps(document))
    assert run(capsys, "launch", "claude", "--model", MODEL)[0] == 0
    assert calls[-1]["SPLASH_API_KEY"] == "sk-splash-test-key-123"


def test_serve_passthrough_warns_when_the_manager_has_the_port(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    from splash_gui.engine.discovery import EngineInfo

    calls: list[list[str]] = []
    monkeypatch.setattr(
        cli_module, "discover", lambda **kwargs: EngineInfo(found=True, cli=Path("/x/splash"))
    )
    monkeypatch.setattr(os, "execv", lambda path, argv: calls.append(argv))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: type("R", (), {"status_code": 200})())
    with pytest.raises(SystemExit):
        cli_module.main(["serve", "--model", "x"])
    assert "Splash GUI is serving on port 8000" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli_module.main(["serve", "--model", "x", "--port=9999"])
    assert "warning" not in capsys.readouterr().err
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("argv", "env", "port"),
    [
        (["--port", "9000"], {}, 9000),
        (["--port=7"], {}, 7),
        ([], {"SPLASH_PORT": "8100"}, 8100),
        ([], {}, 8000),
    ],
)
def test_serve_port(
    argv: list[str], env: dict[str, str], port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SPLASH_PORT", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert cli_module.serve_port(argv) == port

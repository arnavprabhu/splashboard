"""`splash` commands against a manager (SPEC §12.2), with the CLI's HTTP client
pointed at the harness's in-process app."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
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
    monkeypatch.delenv("SPLASH_PORT", raising=False)
    return harness


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli_module.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_ls_load_status_unload(h: EngineHarness, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "ls", "--json")
    assert code == 0 and [m["id"] for m in json.loads(out)] == [MODEL]
    code, out, err = run(capsys, "ls")
    assert code == 0 and MODEL in out and "GGUF" in out
    assert "1 model ·" in err, "the summary line goes to stderr"
    code, out, err = run(capsys, "load", f"{MODEL}:no-think")
    assert code == 0 and f"{MODEL} ready in" in out
    assert "profiles apply per request" in err
    assert h.engine()["state"] in ("ready", "busy") and h.engine()["model"] == MODEL
    code, out, _ = run(capsys, "status")
    assert code == 0 and f"Engine     ready · Splash 1.3.0 · {MODEL}" in out
    assert "Endpoints  OpenAI  http://127.0.0.1:8000/v1" in out
    code, out, _ = run(capsys, "ps")
    assert code == 0 and "IN FLIGHT" in out and MODEL in out and "OpenAI" not in out
    code, out, _ = run(capsys, "status", "--json")
    assert json.loads(out)["engine"]["model"] == MODEL
    code, out, _ = run(capsys, "unload")
    assert code == 0 and "Engine stopped" in out
    assert h.engine()["state"] == "stopped"


def test_config_get_set_unset_validates(
    h: EngineHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "config", "set", "routing.load_timeout", "60")[0] == 0
    code, out, _ = run(capsys, "config", "get", "routing.load_timeout")
    assert code == 0 and float(out) == 60
    # docs/ui/11 §11, §13.2: a validation error is `✗ key: message`, exit 2.
    code, _, err = run(capsys, "config", "set", "serve.queue_size", "0")
    assert code == 2 and err.startswith("✗ ") and "serve.queue_size" in err
    assert h.settings_document()["global"]["serve"]["queue_size"] == 32
    assert run(capsys, "config", "unset", "routing.load_timeout")[0] == 0
    assert h.settings_document()["global"]["routing"]["load_timeout"] == 120


def test_rm_and_version(h: EngineHarness, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "version")
    assert code == 0 and "Splash 1.3.0" in out
    code, out, _ = run(capsys, "rm", MODEL, "--yes")
    assert code == 0 and "Deleted ·" in out and "freed" in out
    assert h.client.get("/api/admin/models").json()["models"] == []
    # §13.2: a model that is not installed is exit 5.
    code, _, err = run(capsys, "rm", MODEL, "--yes")
    assert code == 5 and "not installed" in err.lower()


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
    listing = h.client.post("/api/admin/integrations").json()["cli"]
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
    health = {"status": "ok", "service": "splash-gui-manager", "version": "0.1.0"}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=health))
    with pytest.raises(SystemExit):
        cli_module.main(["serve", "--model", "x"])
    assert "manager is already listening on 8000" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli_module.main(["serve", "--model", "x", "--port=9999"])
    assert "warning" not in capsys.readouterr().err
    # Another server's /health (oMLX on :8000) is not Splash GUI.
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json={"status": "ok"}))
    with pytest.raises(SystemExit):
        cli_module.main(["serve", "--model", "x"])
    assert "already listening" not in capsys.readouterr().err
    assert len(calls) == 3


def _foreign_client(monkeypatch: pytest.MonkeyPatch, seen: list[str]) -> None:
    """The settings port is held by another server whose /health says 200 (oMLX)."""
    real = cli_module.Client

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"state": "ready", "models": []})

    class Client(real):  # type: ignore[misc, valid-type]
        def __init__(self, port: int | None = None) -> None:
            super().__init__(port)
            self.http.close()
            self.http: Any = httpx.Client(base_url=self.url, transport=httpx.MockTransport(answer))

    monkeypatch.setattr(cli_module, "Client", Client)


@pytest.mark.parametrize("argv", [["status"], ["ls"], ["start"], ["stop"], ["load", MODEL]])
def test_another_server_on_the_port_is_not_mistaken_for_the_manager(
    argv: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    _foreign_client(monkeypatch, seen)
    spawned: list[Any] = []
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    code, _, err = run(capsys, *argv)
    assert code == 1
    assert "not Splash GUI" in err and "port 8000" in err and "--port" in err
    assert seen == ["/health"], "no admin call (or CLI token) may reach another server"
    assert spawned == []


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


def test_open_signs_the_browser_in_with_a_one_time_link(
    h: EngineHarness, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D58: `splash open` opens /admin/login?code=… so the browser needs no key."""
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    code, _, _ = run(capsys, "open", "settings")
    assert code == 0 and len(opened) == 1
    url = opened[0]
    prefix = "http://127.0.0.1:8000/admin/login?code="
    assert url.startswith(prefix) and url.endswith("&next=/admin/settings"), url
    login_code = url[len(prefix) :].split("&", 1)[0]
    page = {"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"}
    exchanged = h.client.post(
        "/api/admin/auth/exchange",
        json={"code": login_code},
        headers={**page, "Authorization": ""},
    )
    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.json()["method"] == "session"


@pytest.fixture
def foreign_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, list[tuple[str, str]]]]:
    """A real HTTP server on a loopback port that answers like oMLX: `/health` is
    200 `{"status": "ok"}` and every other path is 200 JSON. It records each request's
    path and Authorization header."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            seen.append((self.path, self.headers.get("Authorization", "")))
            body = b'{"status":"ok"}' if self.path == "/health" else b'{"state":"ready"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_DELETE = _answer

        def log_message(self, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    monkeypatch.setenv("SPLASH_PORT", str(port))
    try:
        yield port, seen
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "argv",
    [["status"], ["--json", "status"], ["ls"], ["load", MODEL], ["open", "settings"]],
)
def test_a_real_foreign_server_on_the_port_is_never_trusted(
    argv: list[str],
    foreign_server: tuple[int, list[tuple[str, str]]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """acceptance-real.md "Port caution": without --port the CLI uses the settings port,
    and oMLX answers /health there with a 200. Over a real socket, the CLI must see that
    it isn't Splash GUI: no admin call, no CLI token, no browser, a clear error."""
    port, seen = foreign_server
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    code, out, err = run(capsys, *argv)
    assert code == 1
    # --json prints the error object on stdout; the human layout uses stderr.
    message = json.loads(out)["error"]["message"] if "--json" in argv else err
    assert "not Splash GUI" in message and f"port {port}" in message, message
    assert seen == [("/health", "")], "only an anonymous /health may reach another server"
    assert opened == []


def test_doctor_reports_a_real_foreign_server_as_a_port_conflict(
    foreign_server: tuple[int, list[tuple[str, str]]], capsys: pytest.CaptureFixture[str]
) -> None:
    port, seen = foreign_server
    _, out, _ = run(capsys, "doctor")
    assert f"Another server (not Splash GUI) answers on port {port}" in out
    assert seen == [("/health", "")]

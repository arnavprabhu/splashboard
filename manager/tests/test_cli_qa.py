"""Regression tests for the CLI QA pass: global options, `ps`, `ls`,
`doctor`, `config get`, `version`, `launch --print`, `--help`, colour, glyphs,
truncation and exit codes. Each was found by running the shim by hand."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from splash_gui import __version__
from splash_gui import cli as cli_module
from splash_gui.cli import doctor as doctor_checks
from splash_gui.cli import output
from splash_gui.engine.discovery import EngineInfo

from .fakeengine import MODEL, EngineHarness

pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")


class _Recording:
    """The harness client, recording every request, minus close()."""

    def __init__(self, inner: Any, seen: list[tuple[str, str]]) -> None:
        self._inner = inner
        self._seen = seen

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self._seen.append((method, url.split("?")[0]))
        return self._inner.request(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:
        self._seen.append(("GET", url))
        return self._inner.get(url, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def close(self) -> None:
        return None


@pytest.fixture
def make(
    harness_factory: Callable[..., EngineHarness],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Callable[..., tuple[EngineHarness, list[tuple[str, str]], list[int | None]]]:
    """A harness plus the requests the CLI sent and the ports it was given."""

    def factory(
        **kwargs: Any,
    ) -> tuple[EngineHarness, list[tuple[str, str]], list[int | None]]:
        harness = harness_factory(**kwargs)
        seen: list[tuple[str, str]] = []
        ports: list[int | None] = []
        real = cli_module.Client

        class Client(real):  # type: ignore[misc, valid-type]
            def __init__(self, port: int | None = None) -> None:
                ports.append(port)
                super().__init__(port)
                self.http.close()
                self.http: Any = _Recording(harness.client, seen)

        monkeypatch.setattr(cli_module, "Client", Client)
        monkeypatch.delenv("SPLASH_PORT", raising=False)
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("LC_CTYPE", raising=False)
        home = tmp_path / "user-home"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        # No real interactive shells: the doctor's shell check sees a clean shell.
        monkeypatch.setattr(doctor_checks, "run_shell", lambda argv: "")
        return harness, seen, ports

    return factory


def _no_spawn(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("the CLI must not start a manager here")


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli_module.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# 1. Global options before (and after) the command ------------------------------------


def test_global_options_before_the_command_are_ours_not_the_engines(
    make: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`splash --port 8123 status` used to fall through to the engine CLI."""
    _, _, ports = make()
    execs: list[list[str]] = []
    monkeypatch.setattr(os, "execve", lambda path, argv, env: execs.append(argv))
    code, out, _ = run(capsys, "--port", "8123", "--json", "status")
    assert execs == [] and code == 0
    assert ports[-1] == 8123
    assert json.loads(out)["manager"]["running"] is True
    code, out, _ = run(capsys, "--json", "ls")
    assert code == 0 and [m["id"] for m in json.loads(out)] == [MODEL]
    code, out, _ = run(capsys, "ls", "--json", "--port=8124")
    assert code == 0 and ports[-1] == 8124 and json.loads(out)[0]["id"] == MODEL
    code, out, _ = run(capsys, "--color", "never", "-q", "version", "--json")
    assert code == 0 and json.loads(out)["engine"] == "1.3.1"
    assert execs == []


def test_engine_commands_still_pass_through_untouched(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    execs: list[list[str]] = []
    monkeypatch.setattr(
        cli_module, "discover", lambda **kwargs: EngineInfo(found=True, cli=Path("/x/splash"))
    )
    monkeypatch.setattr(os, "execve", lambda path, argv, env: execs.append(argv))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(404))
    for argv in (
        ["serve", "--model", "m", "--port", "9999", "--json"],
        ["claude", "--json"],
        ["-h"],
        ["--version"],
        ["--json", "serve", "--model", "m"],
        ["frobnicate", "--port", "1"],
    ):
        with pytest.raises(SystemExit):
            cli_module.main(argv)
        assert execs[-1] == ["/x/splash", *argv]


def test_split_globals() -> None:
    assert cli_module.split_globals(["--port", "8", "--json", "ps", "--wide"]) == (
        ["--port", "8", "--json"],
        ["ps", "--wide"],
    )
    assert cli_module.split_globals(["--color=never", "ls"]) == (["--color=never"], ["ls"])
    assert cli_module.split_globals(["serve", "--port", "8"]) == ([], ["serve", "--port", "8"])
    assert cli_module.split_globals(["--version"]) == ([], ["--version"])


def test_splash_port_environment_selects_the_manager(
    make: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--port` (or SPLASH_PORT)."""
    make()
    monkeypatch.setenv("SPLASH_PORT", "8199")
    real = cli_module.Client.__mro__[1]
    client = real()
    try:
        assert client.port == 8199 and client.url.endswith(":8199")
    finally:
        client.http.close()


# 2. ps ---------------------------------------------------------------------------------


def test_ps_with_the_engine_stopped(make: Any, capsys: pytest.CaptureFixture[str]) -> None:
    make()
    code, out, err = run(capsys, "ps")
    assert (code, out, err) == (0, "", "No model loaded.\n")
    code, out, _ = run(capsys, "ps", "--json")
    assert code == 0 and json.loads(out) == []


def test_ps_table_and_json(make: Any, capsys: pytest.CaptureFixture[str]) -> None:
    h, _, _ = make()
    h.load()
    code, out, _ = run(capsys, "ps")
    assert code == 0
    header, row = out.splitlines()
    assert header.split() == ["MODEL", "STATE", "UPTIME", "IN", "FLIGHT", "QUEUED", "MEMORY"]
    assert row.startswith(MODEL) and " ready " in row
    _, out, _ = run(capsys, "ps", "--json")
    (item,) = json.loads(out)
    assert set(item) == {
        "model",
        "state",
        "uptime_s",
        "in_flight",
        "queued",
        "memory_bytes",
        "limit_bytes",
        "decode_tps",
    }
    assert item["model"] == MODEL and item["state"] in ("ready", "busy")


# 3. ls -----------------------------------------------------------------------------------


def test_ls_has_a_header_marker_format_and_summary(
    make: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    h, _, _ = make()
    _, out, err = run(capsys, "ls")
    header, row = out.splitlines()
    assert header.split() == ["MODEL", "FORMAT", "SIZE", "UNIQUE", "LAST", "USED", "STATUS"]
    assert row.startswith("  " + MODEL)
    assert row.split()[1] == "GGUF" and "GiB" not in row and "never" in row
    assert row.rstrip().endswith("never"), "STATUS is blank when ready"
    assert err.startswith("1 model · ") and " on disk · " in err and err.rstrip().endswith("free")
    h.load()
    _, out, _ = run(capsys, "ls")
    assert out.splitlines()[1].startswith("* " + MODEL)
    assert out.splitlines()[1].rstrip().endswith("active")


def test_ls_json_shape(make: Any, capsys: pytest.CaptureFixture[str]) -> None:
    make()
    _, out, err = run(capsys, "ls", "--json")
    (item,) = json.loads(out)
    assert set(item) == {
        "id",
        "family",
        "format",
        "variant",
        "language_only",
        "size_bytes",
        "unique_bytes",
        "revision",
        "pinned",
        "last_used",
        "status",
    }
    assert "1 model" in err, "the summary goes to stderr, never into the JSON"


def test_ls_empty_state(
    make: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    make(installed=())
    monkeypatch.setattr(cli_module, "recommended_model", lambda client: "a/b-4bit")
    code, out, err = run(capsys, "ls")
    assert (code, out) == (0, "")
    assert err == "No models installed. Download one with: splash pull a/b-4bit\n"


def test_recommended_model_comes_from_the_catalog() -> None:
    catalog = {
        "families": [
            {
                "groups": [
                    {
                        "entries": [
                            {"id": "x/no"},
                            {
                                "id": "u/R-GGUF",
                                "recommended": True,
                                "recommended_variant": "UD-Q4_K_M",
                            },
                        ]
                    }
                ]
            }
        ]
    }

    class Fake:
        def request(self, method: str, path: str) -> Any:
            assert path == "/catalog"
            return catalog

    assert cli_module.recommended_model(Fake()) == "u/R-GGUF:UD-Q4_K_M"  # type: ignore[arg-type]


def test_ls_status_vocabulary_and_sort() -> None:
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = [
        {"id": "b", "status": "ready", "last_used_at": None},
        {"id": "c", "status": "ready", "last_used_at": (now - timedelta(days=1)).isoformat()},
        {"id": "a", "status": "active", "last_used_at": None},
        {"id": "d", "status": "ready", "last_used_at": now.isoformat()},
    ]
    assert [r["id"] for r in cli_module.ls_sorted(rows)] == ["a", "d", "c", "b"]
    status = cli_module.ls_status
    assert status({"status": "ready"}) == ""
    assert status({"status": "ready", "pinned": True}) == "pinned"
    assert status({"status": "downloading", "progress": 0.42}) == "downloading 42 %"
    assert status({"status": "update_available"}) == "update available"
    assert status({"status": "broken"}) == "broken (re-verify)"
    assert status({"status": "unsupported"}) == "no longer loads"


def test_ls_names_an_installed_splash_package_and_never_picks_it() -> None:
    package = {"id": "incoai/Qwen3.8-27B-Splash", "format": "legacy", "status": "unsupported"}
    assert cli_module.format_label(package) == "Package"
    assert cli_module.format_label({"format": "mlx"}) == "MLX"
    assert cli_module.format_label({"format": "gguf"}) == "GGUF"
    newest = {**package, "last_used_at": datetime.now(UTC).isoformat()}
    assert cli_module.most_recent([newest, {"id": "a/b", "status": "ready"}]) == "a/b"


# 4. doctor ---------------------------------------------------------------------------------


def test_doctor_layout_and_no_fix_for_ok_checks(
    make: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    make()
    _, out, _ = run(capsys, "doctor")
    lines = out.splitlines()
    assert lines[0].startswith("Splashboard doctor · ")
    assert "OK" not in out.split() and "WARN" not in out.split()
    assert "Storage" in lines and "Shell" in lines
    assert any(line.startswith("  ✓ ") for line in lines)
    assert "bytes free" not in out and " free" in out
    # An OK check never prints a fix (the manager used to send "chmod 700" for it).
    for index, line in enumerate(lines):
        if line.startswith("  ✓ "):
            following = lines[index + 1] if index + 1 < len(lines) else ""
            assert "Fix:" not in following, line
    assert "chmod" not in out
    assert lines[-1].endswith("passed")


def test_doctor_fails_when_a_shell_function_hides_the_shim(
    make: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user's `splash()` in ~/.zshrc is ✗ and exit 1."""
    make()
    home = Path(os.environ["HOME"])
    (home / ".zshrc").write_text('splash() {\n  command splash "$@" --port 9999\n}\n')
    monkeypatch.setattr(
        doctor_checks,
        "run_shell",
        lambda argv: "splash is a shell function from /x/.zshrc\n" if argv[0] == "zsh" else "",
    )
    code, out, _ = run(capsys, "doctor")
    assert code == 1
    assert "✗ `splash` is hidden by a shell function" in out
    assert "~/.zshrc" in out and "exec zsh" in out and "Fix: " in out
    code, out, _ = run(capsys, "doctor", "--json")
    report = json.loads(out)
    path = next(c for c in report["checks"] if c["id"] == "path")
    assert path["status"] == "fail" and path["group"] == "shell"
    assert any("exec zsh" in fix for fix in path["fix"])
    assert report["summary"]["fail"] >= 1
    assert set(report["checks"][0]) == {"id", "group", "status", "title", "detail", "fix"}


def test_doctor_without_the_function_has_no_shell_problem(
    make: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    make()
    _, out, _ = run(capsys, "doctor", "--json")
    path = next(c for c in json.loads(out)["checks"] if c["id"] == "path")
    assert path["status"] == "ok" and path["fix"] == []


def test_doctor_shell_check_flags_an_earlier_path_hit(paths: Any, tmp_path: Path) -> None:
    output_text = f"splash is /opt/homebrew/bin/splash\nsplash is {paths.shim}\n"
    check = doctor_checks.shell_check(paths, tmp_path, lambda argv: output_text)
    assert check.status == "fail" and "/opt/homebrew/bin/splash" in check.detail[0]
    clean = doctor_checks.shell_check(paths, tmp_path, lambda argv: f"splash is {paths.shim}\n")
    assert clean.status == "ok"


@pytest.mark.parametrize(
    ("free", "status"),
    [(1 * 1024**3, "fail"), (5 * 1024**3, "warn"), (11 * 1024**3, "ok")],
)
def test_doctor_disk_thresholds(
    free: int, status: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.disk_usage", lambda p: type("U", (), {"free": free})())
    check = doctor_checks.disk_check(tmp_path)
    assert check.status == status
    assert "GB free" in check.title and str(free) not in check.title
    assert bool(check.fix) == (status != "ok")


def test_doctor_permissions_fix_only_when_loose(paths: Any) -> None:
    paths.base.chmod(0o700)
    ok = doctor_checks.permissions_check(paths)
    assert ok.status == "ok" and ok.fix == [] and "(0700)" in ok.title
    paths.base.chmod(0o755)
    try:
        loose = doctor_checks.permissions_check(paths)
        assert loose.status == "warn" and "chmod 700" in loose.fix[0]
    finally:
        paths.base.chmod(0o700)


def test_doctor_drops_a_fix_the_manager_sends_for_an_ok_check() -> None:
    (check,) = doctor_checks.from_manager(
        [{"id": "permissions", "label": "Dir", "status": "ok", "message": "m", "fix": "chmod 700"}]
    )
    assert check.fix == []


def test_doctor_ascii_glyphs_outside_utf8() -> None:
    checks = [
        doctor_checks.Check("disk", "ok", "a"),
        doctor_checks.Check("path", "fail", "b", fix=["do it"]),
        doctor_checks.Check("shim", "warn", "c"),
    ]
    lines = doctor_checks.render(checks, output.Style(colour=False, utf8=False))
    assert "  + a" in lines and "  x b" in lines and "  ! c" in lines
    assert "      Fix: do it" in lines
    assert lines[-1] == "1 problem · 1 warning · 1 check passed"


def test_doctor_works_without_the_manager(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    isolated_home: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(doctor_checks, "run_shell", lambda argv: "")
    monkeypatch.setattr(
        cli_module,
        "discover",
        lambda *a, **k: EngineInfo(
            found=True, cli=Path("/x/bin/splash"), version="1.3.1", support="supported"
        ),
    )
    real = cli_module.Client

    class Down(real):  # type: ignore[misc, valid-type]
        def probe(self) -> str:
            return "down"

    monkeypatch.setattr(cli_module, "Client", Down)
    code, out, _ = run(capsys, "doctor")
    assert code == 0
    assert "! Not running" in out and "Fix: splash start" in out
    assert "✓ Splash 1.3.1 · /x/bin/splash" in out


# 5. config get with no key --------------------------------------------------------------


def test_config_get_without_a_key_prints_the_effective_document(
    make: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    make()
    code, out, _ = run(capsys, "config", "get")
    assert code == 0
    assert "server.port = 8000" in out.splitlines()
    assert "serve.max_context = " in out
    code, out, _ = run(capsys, "config", "get", "--json")
    document = json.loads(out)
    assert document["server"]["port"] == 8000 and "routing" in document
    assert "secrets" not in document and "api_key" not in json.dumps(document).replace(
        "api_key_required", ""
    )
    code, out, _ = run(capsys, "config", "get", "routing", "--json")
    assert code == 0 and json.loads(out)["load_timeout"] == 120


def test_config_get_unknown_key_is_a_usage_error(
    make: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    make()
    code, _, err = run(capsys, "config", "get", "no.such.key")
    assert code == 2 and err.startswith("✗ Unknown setting: no.such.key")


def test_config_set_a_per_model_key(make: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """`models."ID".serve.max_context` used to be written under `global.models`."""
    h, _, _ = make()
    code, _, err = run(capsys, "config", "set", f'models."{MODEL}".serve.max_context', "64K")
    assert code == 0, err
    document = h.settings_document()
    assert document["models"][MODEL]["serve"]["max_context"] == "64K"
    assert "models" not in document["global"]
    code, out, _ = run(capsys, "config", "get", f'models."{MODEL}".serve.max_context')
    assert out.strip() == "64K"


# 6. version ---------------------------------------------------------------------------------


def test_version_is_human_readable_unless_json(
    make: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    make()
    code, out, _ = run(capsys, "version")
    assert code == 0 and not out.lstrip().startswith("{")
    assert out.startswith(f"Splashboard {__version__} · manager {__version__} · Splash 1.3.1")
    code, out, _ = run(capsys, "version", "--json")
    assert set(json.loads(out)) == {"gui", "manager", "engine", "engine_path", "status_schema"}


# 7. launch: model choice and --print ---------------------------------------------------------


def _stub_exec(monkeypatch: pytest.MonkeyPatch, h: EngineHarness) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(os, "execve", lambda path, argv, env: calls.append(env))
    monkeypatch.setattr(cli_module, "discover", lambda *a, **k: h.state.engine_cached())
    return calls


def test_launch_print_never_loads_a_model(
    make: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`launch claude --print --model X` used to POST /engine/load (it loaded a
    model on the real manager during the QA pass)."""
    h, seen, _ = make()
    calls = _stub_exec(monkeypatch, h)
    for argv in (
        ["launch", "claude", "--print"],
        ["launch", "claude", "--print", "--model", MODEL],
    ):
        code, out, err = run(capsys, *argv)
        assert code == 0, err
        assert json.loads(calls[-1]["SPLASH_GUI_CLIENT_SPEC"])["model"] == MODEL
        assert out.startswith("# splash launch claude --print  (Splash 1.3.1 launcher")
        assert "not loaded now" in out
    assert not [s for s in seen if s[0] != "GET"], seen
    assert h.engine()["state"] == "stopped"


def test_launch_print_with_nothing_to_print_is_exit_2(
    make: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    h, _, _ = make(installed=())
    _stub_exec(monkeypatch, h)
    code, _, err = run(capsys, "launch", "claude", "--print")
    assert code == 2
    assert err.startswith(
        "✗ No model is loaded. Pass --model <ID> or set a default: "
        "splash config set routing.default_model <ID>"
    )


def test_launch_without_a_tty_and_no_model_is_exit_2(
    make: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    h, seen, _ = make()
    _stub_exec(monkeypatch, h)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code, _, err = run(capsys, "launch", "codex")
    assert code == 2 and "Pass --model <ID>" in err
    assert ("POST", "/api/admin/engine/load") not in seen


def test_launch_on_a_tty_asks_with_a_numbered_choice(
    make: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    h, _, _ = make()
    calls = _stub_exec(monkeypatch, h)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", answer)
    code, _, err = run(capsys, "launch", "codex")
    assert code == 0, err
    assert "No model is loaded. Which one should Splash serve?" in err
    assert f"1) {MODEL}" in err and prompts == ["Choose [1]: "]
    assert json.loads(calls[-1]["SPLASH_GUI_CLIENT_SPEC"])["model"] == MODEL
    assert h.engine()["model"] == MODEL
    assert "▸ codex on " in err and "session only" in err


def test_launch_uses_the_routing_default_without_asking(
    make: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    h, _, _ = make()
    calls = _stub_exec(monkeypatch, h)
    assert run(capsys, "config", "set", "routing.default_model", MODEL)[0] == 0
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked: " + prompt))
    code, _, err = run(capsys, "launch", "codex")
    assert code == 0, err
    assert json.loads(calls[-1]["SPLASH_GUI_CLIENT_SPEC"])["model"] == MODEL


# 8. --help ----------------------------------------------------------------------------------


def test_long_help_is_ours(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "engine_commands", lambda: ("serve", "claude", "newcmd"))
    code, out, _ = run(capsys, "--help")
    assert code == 0
    assert out.startswith("Splashboard — run, monitor and chat with Splash models")
    for heading in ("Serving", "Models", "Use", "Maintenance", "Options"):
        assert heading in out.splitlines()
    assert "--port N" in out and "--json" in out
    assert "Engine commands (passed to Splash): serve, claude, newcmd, --version, -h" in out
    code, out, _ = run(capsys)
    assert code == 0 and "Engine commands (passed to Splash)" in out


# Colour, glyphs, formatters, truncation, exit codes -------------------------------------------


class _Tty:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


@pytest.mark.parametrize(
    ("choice", "tty", "env", "expected"),
    [
        ("auto", True, {}, True),
        ("auto", False, {}, False),
        ("auto", True, {"NO_COLOR": "1"}, False),
        ("auto", True, {"NO_COLOR": ""}, True),
        ("always", False, {"NO_COLOR": "1"}, True),
        ("never", True, {}, False),
    ],
)
def test_colour_rules(choice: str, tty: bool, env: dict[str, str], expected: bool) -> None:
    assert output.colour_enabled(choice, _Tty(tty), env) is expected  # type: ignore[arg-type]


def test_no_color_status_has_no_escapes(
    make: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    h, _, _ = make()
    h.load()
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    _, out, _ = run(capsys, "status")
    assert "\033[" not in out
    monkeypatch.delenv("NO_COLOR")
    _, out, _ = run(capsys, "--color", "always", "ps")
    assert "\033[1;38;5;166mready\033[0m" in out


@pytest.mark.parametrize(
    ("env", "utf8"),
    [
        ({"LANG": "en_US.UTF-8"}, True),
        ({"LANG": "C"}, False),
        ({"LC_ALL": "C", "LANG": "en_US.UTF-8"}, False),
        ({"LC_CTYPE": "UTF-8", "LANG": "C"}, True),
        ({}, False),
    ],
)
def test_glyphs_follow_the_locale(env: dict[str, str], utf8: bool) -> None:
    assert output.utf8_locale(env) is utf8
    style = output.Style(utf8=output.utf8_locale(env))
    assert style.glyph("ok") == ("✓" if utf8 else "+")
    assert style.glyph("fail") == ("✗" if utf8 else "x")


def test_formatters_match_the_web() -> None:
    assert output.fmt_bytes(14256066486) == "13.3 GB"
    assert output.fmt_bytes(0) == "0 B"
    assert output.fmt_bytes(512 * 1024**2) == "512 MB"
    assert output.fmt_bytes(1177757982720) == "1.1 TB"
    assert output.fmt_bytes(None) == "—"
    assert output.fmt_duration(45) == "45 s"
    assert output.fmt_duration(200) == "3 m 20 s"
    assert output.fmt_duration(7500) == "2 h 05 m"
    assert output.fmt_duration(3 * 86400 + 4 * 3600) == "3 d 4 h"
    assert output.fmt_tokens(131072) == "128K"
    assert output.fmt_tps(74.24) == "74.2 tok/s" and output.fmt_tps(3912) == "3,912 tok/s"
    assert output.fmt_ms(310) == "310 ms" and output.fmt_ms(1240) == "1.24 s"
    assert output.fmt_percent(0.875) == "87.5%"
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    assert output.fmt_relative((now - timedelta(minutes=2)).isoformat(), now) == "2 min ago"
    assert output.fmt_relative((now - timedelta(days=1)).isoformat(), now) == "yesterday"
    assert output.fmt_relative("2026-09-20T12:00:00+00:00", now) == "2026-09-20"


def test_tables_truncate_the_model_column_in_the_middle_only_when_needed() -> None:
    long_id = "mlx-community/Qwen3.8-27B-Instruct-Extra-Long-Name-4bit"
    table = output.Table(["MODEL", "STATE"], rows=[[long_id, "ready"]])
    lines = table.render(output.Style(), width=40)
    assert all(len(line) <= 40 for line in lines)
    row = lines[1]
    assert row.startswith("mlx-community/") and "…" in row and "4bit" in row
    wide = output.Table(["MODEL", "STATE"], rows=[[long_id, "ready"]]).render(output.Style())
    assert long_id in wide[1], "no width (pipe, --wide): never truncated"


def test_piped_ls_never_truncates_ids(
    make: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    make()
    monkeypatch.setattr("shutil.get_terminal_size", lambda *a: os.terminal_size((30, 24)))
    _, out, _ = run(capsys, "ls")
    assert MODEL in out
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    _, out, _ = run(capsys, "ls")
    assert MODEL not in out and "…" in out
    _, out, _ = run(capsys, "ls", "--wide")
    assert MODEL in out


@pytest.mark.parametrize(
    ("status", "code", "exit_code"),
    [
        (404, "model_not_found", 5),
        (404, "model_not_installed", 5),
        (422, "incompatible", 5),
        (409, "model_switch_busy", 7),
        (503, "model_switch_busy", 7),
        (409, "install_in_progress", 7),
        (503, "install_in_progress", 7),
        (503, "engine_unavailable", 4),
        (503, "engine_not_found", 6),
        (401, "auth_required", 8),
        (403, "host_not_allowed", 8),
        (409, "download_active", 2),
        (422, "invalid_settings", 2),
        (500, "whatever", 1),
    ],
)
def test_manager_errors_map_to_exit_codes(status: int, code: str, exit_code: int) -> None:
    response = httpx.Response(status, json={"error": {"message": "m", "type": "t", "code": code}})
    assert cli_module.api_failure(response).exit_code == exit_code


def test_json_errors_go_to_stdout(make: Any, capsys: pytest.CaptureFixture[str]) -> None:
    make()
    code, out, err = run(capsys, "--json", "rm", "nobody/nothing", "--yes")
    assert code == 5 and err == ""
    assert set(json.loads(out)["error"]) >= {"code", "message", "fix"}


def test_read_only_commands_never_start_the_manager(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    real = cli_module.Client

    class Down(real):  # type: ignore[misc, valid-type]
        def probe(self) -> str:
            return "down"

    monkeypatch.setattr(cli_module, "Client", Down)
    for argv in (["ps"], ["ls"], ["logs"], ["config", "get"], ["unload"], ["rm", "a/b", "--yes"]):
        code, out, err = run(capsys, *argv)
        assert code == 3, argv
        assert err.startswith("✗ Splashboard is not running") and "splash start" in err
    code, out, _ = run(capsys, "status", "--json")
    assert code == 3 and json.loads(out)["manager"]["running"] is False


def test_rm_without_a_tty_names_the_flag(
    make: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No TTY to ask → exit 2 and the flag to pass instead (was a
    silent exit 1)."""
    h, _, _ = make()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code, _, err = run(capsys, "rm", MODEL)
    assert code == 2 and "--yes" in err
    assert h.client.get("/api/admin/models").json()["models"]


def test_quiet_suppresses_notes(make: Any, capsys: pytest.CaptureFixture[str]) -> None:
    make()
    _, _, err = run(capsys, "ls")
    assert "1 model" in err
    _, _, err = run(capsys, "--quiet", "ls")
    assert err == ""


def test_install_in_progress_tells_a_terminal_user_what_to_do() -> None:
    """Review finding: `splash load/run/launch` printed the manager's "Send force to
    stop it anyway" though the CLI has no --force for it."""
    response = httpx.Response(
        409,
        json={
            "error": {
                "message": "Splash is downloading o/r for o/r:Q; stopping it now restarts the "
                "file in progress from the beginning. Send force to stop it anyway",
                "type": "conflict",
                "code": "install_in_progress",
                "details": {"active": "o/r:Q", "repo": "o/r", "done_bytes": 1, "total_bytes": 2},
            }
        },
    )
    failure = cli_module.api_failure(response)
    assert failure.code == "install_in_progress"
    assert failure.headline == "Splash is downloading o/r for o/r:Q"
    assert "force" not in failure.headline.lower() and "force" not in (failure.fix or "")
    assert failure.fix == (
        "wait for the download, or run `splash unload` to stop it (the file in progress restarts)"
    )

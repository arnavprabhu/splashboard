"""`helpers/launch_client.py` against Splash's own `install/clients.py`.

The helper runs under Splash's Python with `PYTHONPATH=PKG`. Here it runs under
the test interpreter with `PYTHONPATH` at the reference clone (or the installed
package), so it exercises the real configurator, and a throwaway HOME proves
that a CLI session writes nothing (session-only).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from .conftest import REPO, SPLASH_PKG, write_script

HELPER = REPO / "manager" / "splash_gui" / "helpers" / "launch_client.py"
_CANDIDATES = (REPO / "splash", SPLASH_PKG)
PKG = next((p for p in _CANDIDATES if (p / "install" / "clients.py").exists()), None)
pytestmark = pytest.mark.skipif(PKG is None, reason="no Splash install/clients.py to run against")
MODEL = "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M:no-think"


def files_under(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def run(
    tmp_path: Path,
    client: str,
    *,
    print_only: bool,
    args: list[str] | None = None,
    path_dirs: list[Path] | None = None,
    extra_env: dict[str, str] | None = None,
    effort: str | None = None,
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    spec: dict[str, Any] = {
        "client": client,
        "model": MODEL,
        "url": "http://127.0.0.1:8000",
        "context": 131072,
        "modalities": ["text", "image"],
        "args": args or [],
        "print": print_only,
        "reasoning_effort": effort,
    }
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join([*(str(d) for d in path_dirs or []), "/usr/bin", "/bin"]),
        "PYTHONPATH": str(PKG),
        "SPLASH_GUI_CLIENT_SPEC": json.dumps(spec),
        "ANTHROPIC_API_KEY": "sk-user-real-key",
        **(extra_env or {}),
    }
    return subprocess.run(
        [sys.executable, str(HELPER)], env=env, capture_output=True, text=True, timeout=30
    )


def test_claude_print_is_environment_only(tmp_path: Path) -> None:
    result = run(tmp_path, "claude", print_only=True)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "export ANTHROPIC_BASE_URL=http://127.0.0.1:8000" in out
    assert f"export ANTHROPIC_MODEL={MODEL}" in out, "the profile ID reaches the client"
    assert f"export ANTHROPIC_DEFAULT_SONNET_MODEL={MODEL}" in out
    assert "export ANTHROPIC_AUTH_TOKEN=local" in out, "no key set: Splash's placeholder"
    assert "sk-user-real-key" not in out
    assert f"--model {MODEL}" in out
    assert files_under(tmp_path / "home") == [], "a CLI session never writes config"


def test_print_never_shows_the_api_key(tmp_path: Path) -> None:
    result = run(tmp_path, "claude", print_only=True, extra_env={"SPLASH_API_KEY": "k-secret"})
    assert result.returncode == 0, result.stderr
    assert "k-secret" not in result.stdout
    assert 'export ANTHROPIC_AUTH_TOKEN="${SPLASH_API_KEY}"' in result.stdout


def test_codex_print_uses_only_overrides(tmp_path: Path) -> None:
    result = run(tmp_path, "codex", print_only=True, args=["exec", "hi"])
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert f'model="{MODEL}"' in out
    assert 'wire_api="responses"' in out and "model_context_window=131072" in out
    assert out.rstrip().endswith("exec hi")
    assert files_under(tmp_path / "home") == []


def test_codex_session_turns_apps_off(tmp_path: Path) -> None:
    """Splash rejects tool names over 64 characters and Codex sends ChatGPT
    Apps connectors as tools, so the session runs with `features.apps=false`."""
    result = run(tmp_path, "codex", print_only=True, args=["exec", "hi"])
    assert result.returncode == 0, result.stderr
    assert "-c features.apps=false exec hi" in result.stdout
    assert files_under(tmp_path / "home") == [], "a -c override, never config.toml"


def test_a_user_s_own_apps_override_still_wins(tmp_path: Path) -> None:
    """Codex applies `-c` left to right (last wins); ours goes before the user's."""
    result = run(tmp_path, "codex", print_only=True, args=["-c", "features.apps=true", "exec"])
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.index("features.apps=false") < out.index("features.apps=true")


@pytest.mark.parametrize("client", ["claude", "opencode"])
def test_other_clients_get_no_codex_override(tmp_path: Path, client: str) -> None:
    result = run(tmp_path, client, print_only=True)
    assert result.returncode == 0, result.stderr
    assert "features.apps" not in result.stdout


def test_codex_launch_execs_with_apps_off(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    record = tmp_path / "record.json"
    write_script(
        bin_dir / "codex",
        f'"{sys.executable}" -c \'import json,sys; '
        f'json.dump(sys.argv[1:], open("{record}", "w"))\' "$@"\n',
    )
    result = run(tmp_path, "codex", print_only=False, args=["exec", "hi"], path_dirs=[bin_dir])
    assert result.returncode == 0, result.stderr
    argv = json.loads(record.read_text())
    i = argv.index("features.apps=false")
    assert argv[i - 1] == "-c" and argv[-2:] == ["exec", "hi"]
    assert files_under(tmp_path / "home") == []


def test_session_args_only_touch_codex() -> None:
    sys.path.insert(0, str(HELPER.parent))
    try:
        import launch_client  # type: ignore[import-not-found]
    finally:
        sys.path.remove(str(HELPER.parent))
    assert launch_client.session_args("codex", ["exec"]) == ["-c", "features.apps=false", "exec"]
    assert launch_client.session_args("claude", ["-p", "x"]) == ["-p", "x"]


def test_opencode_print_is_inline_config(tmp_path: Path) -> None:
    result = run(tmp_path, "opencode", print_only=True)
    assert result.returncode == 0, result.stderr
    assert "export OPENCODE_CONFIG_CONTENT=" in result.stdout
    assert f"splash/{MODEL}" in result.stdout
    assert files_under(tmp_path / "home") == []


def test_opencode_2_gets_standalone(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    write_script(bin_dir / "opencode", 'echo "2.1.0"\n')
    result = run(tmp_path, "opencode", print_only=True, path_dirs=[bin_dir])
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip().endswith("--standalone"), result.stdout


@pytest.mark.parametrize("client", ["hermes", "pi"])
def test_hermes_and_pi_previews_write_nothing(tmp_path: Path, client: str) -> None:
    result = run(tmp_path, client, print_only=True)
    assert result.returncode == 0, result.stderr
    assert f"--model {MODEL}" in result.stdout
    assert "defaults stay unchanged" in result.stdout
    assert files_under(tmp_path / "home") == []


def test_claude_launch_execs_the_client_with_a_private_environment(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    record = tmp_path / "record.json"
    write_script(
        bin_dir / "claude",
        f'"{sys.executable}" -c \'import json,os,sys; '
        f'json.dump({{"argv": sys.argv[1:], "env": dict(os.environ)}}, '
        f'open("{record}", "w"))\' "$@"\n',
    )
    result = run(tmp_path, "claude", print_only=False, args=["-p", "hello"], path_dirs=[bin_dir])
    assert result.returncode == 0, result.stderr
    seen = json.loads(record.read_text())
    assert seen["argv"][-2:] == ["-p", "hello"]
    assert seen["argv"][seen["argv"].index("--model") + 1] == MODEL
    env = seen["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8000"
    assert env["ANTHROPIC_MODEL"] == MODEL
    assert "ANTHROPIC_API_KEY" not in env, "the user's real key never reaches the session"
    assert "SPLASH_GUI_CLIENT_SPEC" not in env
    assert files_under(tmp_path / "home") == []


def test_a_missing_client_says_to_install_it(tmp_path: Path) -> None:
    result = run(tmp_path, "claude", print_only=False)
    assert result.returncode != 0
    assert "Install claude before launching it" in result.stderr


def test_the_session_environment_keeps_the_user_s_pythonpath(tmp_path: Path) -> None:
    """Splash's package reaches the helper through sys.path (as Splash's own
    launcher does), never as PYTHONPATH in the client and the tools it runs."""
    bin_dir = tmp_path / "bin"
    record = tmp_path / "record.json"
    write_script(
        bin_dir / "claude",
        f'"{sys.executable}" -c \'import json,os; '
        f'json.dump(dict(os.environ), open("{record}", "w"))\'\n',
    )
    home = tmp_path / "home"
    home.mkdir()
    spec = {
        "client": "claude",
        "model": MODEL,
        "url": "http://127.0.0.1:8000",
        "context": 131072,
        "modalities": ["text"],
        "args": [],
        "print": False,
    }
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
        "PYTHONPATH": "/the/user/own",
        "SPLASH_GUI_ENGINE_PKG": str(PKG),
        "SPLASH_GUI_CLIENT_SPEC": json.dumps(spec),
    }
    result = subprocess.run(
        [sys.executable, str(HELPER)], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    seen = json.loads(record.read_text())
    assert seen["PYTHONPATH"] == "/the/user/own"
    assert "SPLASH_GUI_ENGINE_PKG" not in seen
    assert seen["ANTHROPIC_MODEL"] == MODEL


FAKE_PKG = REPO / "scripts" / "fake_splash" / "pkg"


@pytest.mark.parametrize("client", ["claude", "codex", "opencode", "hermes", "pi"])
def test_print_works_on_the_fake_engine(tmp_path: Path, client: str) -> None:
    """The fake engine ships `install/clients.py` (a verbatim 1.3.0 copy), so
    `splash launch <client> --print` works there as on the real engine."""
    home = tmp_path / "home"
    home.mkdir()
    spec = {
        "client": client,
        "model": MODEL,
        "url": "http://127.0.0.1:8000",
        "context": 131072,
        "modalities": ["text"],
        "args": [],
        "print": True,
    }
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "SPLASH_GUI_ENGINE_PKG": str(FAKE_PKG),
        "SPLASH_GUI_CLIENT_SPEC": json.dumps(spec),
    }
    result = subprocess.run(
        [sys.executable, str(HELPER)], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert MODEL in result.stdout
    assert files_under(home) == [], "--print writes nothing"


# A profile's reasoning effort becomes the client's own per-run option.


def _helper() -> Any:
    sys.path.insert(0, str(HELPER.parent))
    try:
        import launch_client
    finally:
        sys.path.remove(str(HELPER.parent))
    return launch_client


@pytest.mark.parametrize(
    ("client", "effort", "expected"),
    [
        ("hermes", "none", ["--reasoning", "none"]),
        ("hermes", "max", ["--reasoning", "max"]),
        ("pi", "none", ["--thinking", "off"]),
        ("pi", "low", ["--thinking", "low"]),
        ("codex", "none", ["-c", 'model_reasoning_effort="none"']),
        ("claude", "high", ["--effort", "high"]),
        ("claude", "none", []),  # an environment variable instead
        ("claude", "minimal", []),  # Claude Code has no "minimal"
        ("opencode", "none", []),  # sends no effort; the manager injects it
        ("hermes", None, []),
    ],
)
def test_reasoning_args_per_client(client: str, effort: str | None, expected: list[str]) -> None:
    assert _helper().reasoning_args(client, effort, []) == expected


def test_a_user_s_own_reasoning_flag_wins() -> None:
    helper = _helper()
    assert helper.reasoning_args("hermes", "none", ["--reasoning", "high", "-z", "x"]) == []
    assert helper.reasoning_args("pi", "none", ["--thinking=high"]) == []
    assert helper.reasoning_args("claude", "low", ["--effort", "max"]) == []
    # Only the user's own options count, not a prompt after `--`.
    assert helper.reasoning_args("hermes", "none", ["--", "--reasoning"]) == [
        "--reasoning",
        "none",
    ]
    assert helper.reasoning_env("claude", "none", {"MAX_THINKING_TOKENS": "4096"}) == {}
    assert helper.reasoning_env("claude", "none", {}) == {"MAX_THINKING_TOKENS": "0"}
    assert helper.reasoning_env("hermes", "none", {}) == {}


@pytest.mark.parametrize(
    ("client", "flag"), [("hermes", "--reasoning none -z hi"), ("pi", "--thinking off -z hi")]
)
def test_hermes_and_pi_previews_show_the_reasoning_flag(
    tmp_path: Path, client: str, flag: str
) -> None:
    result = run(tmp_path, client, print_only=True, args=["-z", "hi"], effort="none")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0].endswith(f"--model {MODEL} {flag}")
    assert files_under(tmp_path / "home") == []


def test_codex_effort_override_comes_before_the_user_s(tmp_path: Path) -> None:
    args = ["-c", 'model_reasoning_effort="high"', "exec", "hi"]
    result = run(tmp_path, "codex", print_only=True, args=args, effort="none")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.index('model_reasoning_effort="none"') < out.index(
        'model_reasoning_effort="high"'
    ), "Codex takes the last -c, so the user's own still wins"
    assert files_under(tmp_path / "home") == [], "a -c override, never config.toml"


def test_claude_no_think_session_turns_thinking_off(tmp_path: Path) -> None:
    """Claude Code sends `thinking: adaptive` unless MAX_THINKING_TOKENS=0, and Splash
    reads a Messages request without `thinking` as off (server/api_shapes.py
    _anthropic_thinking)."""
    bin_dir = tmp_path / "bin"
    record = tmp_path / "record.json"
    write_script(
        bin_dir / "claude",
        f'"{sys.executable}" -c \'import json,os,sys; '
        f'json.dump({{"argv": sys.argv[1:], "env": dict(os.environ)}}, '
        f'open("{record}", "w"))\' "$@"\n',
    )
    result = run(
        tmp_path, "claude", print_only=False, args=["-p", "hi"], path_dirs=[bin_dir], effort="none"
    )
    assert result.returncode == 0, result.stderr
    seen = json.loads(record.read_text())
    assert seen["env"]["MAX_THINKING_TOKENS"] == "0"
    assert "--effort" not in seen["argv"]
    assert files_under(tmp_path / "home") == []
    printed = run(tmp_path, "claude", print_only=True, effort="low")
    assert "--effort low" in printed.stdout and "MAX_THINKING_TOKENS" not in printed.stdout


def test_pi_launch_passes_thinking_off_and_writes_no_thinking_setting(tmp_path: Path) -> None:
    """Pi's `--thinking` is per run (it never calls setDefaultThinkingLevel), and
    Splash's provider entry carries no effort: nothing about it reaches a file."""
    bin_dir = tmp_path / "bin"
    record = tmp_path / "record.json"
    write_script(
        bin_dir / "pi",
        f'"{sys.executable}" -c \'import json,sys; '
        f'json.dump(sys.argv[1:], open("{record}", "w"))\' "$@"\n',
    )
    result = run(
        tmp_path, "pi", print_only=False, args=["-p", "hi"], path_dirs=[bin_dir], effort="none"
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(record.read_text())
    assert argv[argv.index("--thinking") + 1] == "off"
    assert argv[-2:] == ["-p", "hi"]
    written = files_under(tmp_path / "home")
    assert written == [".pi", ".pi/agent", ".pi/agent/models.json"], "only Splash's provider"
    models = (tmp_path / "home" / ".pi" / "agent" / "models.json").read_text()
    assert "reasoning_effort" not in models and '"off": "none"' in models

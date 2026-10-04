"""`helpers/launch_client.py` against Splash's own `install/clients.py` (SPEC §11.2, D18).

The helper runs under Splash's Python with `PYTHONPATH=PKG`. Here it runs under
the test interpreter with `PYTHONPATH` at the reference clone (or the installed
package), so it exercises the real configurator, and a throwaway HOME proves
that a CLI session writes nothing (session-only, D18).
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
    assert files_under(tmp_path / "home") == [], "a CLI session never writes config (D18)"


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
    """D18: Splash's package reaches the helper through sys.path (as Splash's own
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

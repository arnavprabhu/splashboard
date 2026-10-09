"""Finding the user's own programs from a LaunchAgent, whose PATH has only system folders."""

from __future__ import annotations

from pathlib import Path

import pytest

from splash_gui.system import userpath


def _program(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_a_program_in_a_user_folder_is_found_with_a_system_only_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    claude = _program(tmp_path / ".local" / "bin" / "claude")
    opencode = _program(tmp_path / ".opencode" / "bin" / "opencode")
    assert userpath.which("claude", tmp_path) == str(claude)
    assert userpath.which("opencode", tmp_path) == str(opencode)
    assert userpath.which("not-installed", tmp_path) is None


def test_the_login_shell_path_is_searched_after_the_managers_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = _program(tmp_path / "tools" / "codex").parent
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(userpath, "shell_path", lambda: f"{custom}:/usr/bin")
    parts = userpath.user_path(tmp_path).split(":")
    assert parts[:3] == ["/usr/bin", "/bin", str(custom)]
    assert parts.count("/usr/bin") == 1
    assert userpath.which("codex", tmp_path) == str(custom / "codex")


def test_the_shell_path_ignores_what_rc_files_print_first(monkeypatch: pytest.MonkeyPatch) -> None:
    class Done:
        stdout = "Welcome!\n__PATH__/a:/b\n"

    monkeypatch.setattr("splash_gui.system.userpath.subprocess.run", lambda *a, **k: Done())
    assert userpath._shell_path() == "/a:/b"

"""The shim's place on PATH and its fallback when Splashboard.app is gone.

The shell checks run the real zsh and bash with a cleared environment and a throwaway HOME.
`brew_shellenv` writes the PATH line that `brew shellenv` prints, for a fake prefix, so the
test does not need Homebrew.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from splash_gui.cli import install as shim
from splash_gui.paths import Paths

CLEAN_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
NEEDS_SHELLS = pytest.mark.skipif(
    shutil.which("zsh") is None or shutil.which("bash") is None, reason="needs zsh and bash"
)


def fake_engine(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    engine = directory / "splash"
    engine.write_text('#!/bin/sh\necho "engine: $*"\n')
    engine.chmod(0o755)
    return engine


def fake_brew(prefix: Path) -> str:
    """A Homebrew stand-in under `prefix`: `brew shellenv` prints the same PATH export as the
    real one (it puts the prefix's bin first, every time), and the prefix has a `splash`.
    Returns the line the user's rc file has, `eval "$(<prefix>/bin/brew shellenv)"`."""
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    export = f'export PATH="{prefix}/bin:{prefix}/sbin${{PATH+:$PATH}}";'
    brew = bin_dir / "brew"
    brew.write_text(f"#!/bin/sh\n[ \"$1\" = shellenv ] && echo '{export}'\n")
    brew.chmod(0o755)
    fake_engine(bin_dir)
    return f'eval "$({prefix}/bin/brew shellenv)"\n'


def wire_home(home: Path, before: dict[str, str], after: dict[str, str]) -> None:
    """What the wizard leaves behind: the user's own lines, the PATH block in every rc file
    (`add_to_path`), then whatever a later edit appended after the block."""
    home.mkdir(parents=True, exist_ok=True)
    for name, text in before.items():
        (home / name).write_text(text)
    shim.install_shim(Paths(home / ".splash"), ["/bin/true", "splash"])
    for name in shim.RC_FILES:
        shim.add_to_rc(home / name, home / ".splash" / "bin")
    for name, text in after.items():
        with (home / name).open("a") as handle:
            handle.write(text)


def first_on_path(home: Path, shell: str, flags: str) -> str:
    """The first `splash` that `type -a` finds in that kind of shell, or ''."""
    result = subprocess.run(
        [shell, flags, "type -a splash"],
        env={"HOME": str(home), "PATH": CLEAN_PATH},
        cwd=home,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[0].removeprefix("splash is ") if lines else ""


# (shell, flags, the rc file that holds brew shellenv, where it sits relative to our block).
# zsh -lic is a login interactive shell (Terminal.app); -ic is a nested interactive shell.
# A brew line appended after the block in ~/.bash_profile is not in this list: bash's login
# shell does not read ~/.bashrc, so nothing comes after it. The doctor reports that case.
POSITIONS = [
    ("zsh", "-lic", ".zprofile", "before"),
    ("zsh", "-lic", ".zshrc", "before"),
    ("zsh", "-lic", ".zprofile", "after"),  # ~/.zshrc, read last, puts the shim back first
    ("zsh", "-ic", ".zprofile", "before"),
    ("zsh", "-ic", ".zshrc", "before"),
    ("zsh", "-ic", ".zprofile", "after"),
    ("bash", "-lic", ".bash_profile", "before"),
    ("bash", "-lic", ".bashrc", "before"),
    ("bash", "-ic", ".bash_profile", "before"),
    ("bash", "-ic", ".bashrc", "before"),
]


@NEEDS_SHELLS
@pytest.mark.parametrize(
    ("shell", "flags", "brew_file", "when"),
    POSITIONS,
    ids=[f"{s}{f}-{b}-{w}" for s, f, b, w in POSITIONS],
)
def test_the_shim_is_first_on_path_wherever_brew_shellenv_is(
    tmp_path: Path, shell: str, flags: str, brew_file: str, when: str
) -> None:
    line = fake_brew(tmp_path / "brew")
    before = {brew_file: line} if when == "before" else {}
    after = {} if when == "before" else {brew_file: line}
    home = tmp_path / "home"
    wire_home(home, before, after)
    assert first_on_path(home, shell, flags) == str(home / ".splash" / "bin" / "splash")


@NEEDS_SHELLS
def test_the_trunk_layout_loses_to_a_brew_line_in_zshrc(tmp_path: Path) -> None:
    """The trunk layout (block in ~/.zprofile only) is the failure this PR fixes: a
    `brew shellenv` in ~/.zshrc puts Homebrew ahead of the shim in a login shell."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zprofile").write_text(
        "# >>> splash gui (managed block, do not edit) >>>\n"
        'export PATH="$HOME/.splash/bin:$PATH"\n'
        "# <<< splash gui <<<\n"
    )
    (home / ".zshrc").write_text(fake_brew(tmp_path / "brew"))
    shim.install_shim(Paths(home / ".splash"), ["/bin/true", "splash"])
    assert first_on_path(home, "zsh", "-lic") != str(home / ".splash" / "bin" / "splash")


def packaged_shim(tmp_path: Path) -> tuple[Path, Path]:
    """A shim written for a bundle, with the bundle missing. Returns (shim, bundled python)."""
    python = tmp_path / "Test.app/Contents/Resources/manager/python/bin/python3"
    script = tmp_path / "bin" / "splash"
    script.parent.mkdir(parents=True)
    script.write_bytes(shim.render([str(python), "-I", "-B", "-m", "splash_gui.cli"]))
    script.chmod(0o755)
    return script, python


def run_sh(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(script), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )


def test_the_moved_app_shim_says_why_our_commands_cannot_run(tmp_path: Path) -> None:
    script, _ = packaged_shim(tmp_path)
    for args in (["status"], ["--json", "status"], ["--port", "8000", "doctor"]):
        result = run_sh(script, *args, env={"PATH": CLEAN_PATH})
        assert result.returncode == 1, args
        assert "Splashboard.app was moved or deleted" in result.stderr
        assert "command splash" in result.stderr
        assert result.stdout == "", "our commands print nothing to stdout when they cannot run"


def test_the_moved_app_shim_still_runs_engine_commands(tmp_path: Path) -> None:
    script, _ = packaged_shim(tmp_path)
    engine = fake_engine(tmp_path / "engine")
    env = {"PATH": CLEAN_PATH, "SPLASH_GUI_REAL_SPLASH": str(engine)}
    cases = {
        "--version": "engine: --version",
        "serve --port 9999 --model m": "engine: serve --port 9999 --model m",
    }
    for command, expected in cases.items():
        result = run_sh(script, *command.split(), env=env)
        assert result.returncode == 0, command
        assert result.stdout.strip() == expected


def test_the_moved_app_shim_finds_the_engine_on_path_past_the_shims(tmp_path: Path) -> None:
    script, _ = packaged_shim(tmp_path)
    engine = fake_engine(tmp_path / "engine")
    path = os.pathsep.join([str(script.parent), str(engine.parent), "/usr/bin", "/bin"])
    result = run_sh(script, "--version", env={"PATH": path})
    assert result.returncode == 0
    assert result.stdout.strip() == "engine: --version"


def test_the_moved_app_shim_says_how_to_install_when_no_engine_is_found(tmp_path: Path) -> None:
    script, _ = packaged_shim(tmp_path)
    result = run_sh(script, "serve", env={"PATH": str(script.parent)})
    assert result.returncode == 127
    assert "brew install incoai/tap/splash" in result.stderr


def test_with_the_app_the_bundled_interpreter_runs_every_command(tmp_path: Path) -> None:
    script, python = packaged_shim(tmp_path)
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\necho "bundled: $*"\n')
    python.chmod(0o755)
    result = run_sh(script, "status", env={"PATH": CLEAN_PATH})
    assert result.returncode == 0
    assert result.stdout.strip() == "bundled: -I -B -m splash_gui.cli status"


def test_the_packaged_shim_is_posix_sh(tmp_path: Path) -> None:
    script, _ = packaged_shim(tmp_path)
    syntax = subprocess.run(["/bin/sh", "-n", str(script)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

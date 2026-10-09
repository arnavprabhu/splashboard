"""`splash doctor`'s shell check: the file and line that hide the shim, and the fix for each
case (SPEC §12.1, PKG-11). The owner's `splash()` in ~/.zshrc is the case to get exactly right."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from splash_gui.cli import doctor as checks
from splash_gui.cli import install as shim
from splash_gui.paths import Paths


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def runner_for(zsh: str = "", bash: str = ""):
    def run(argv: list[str]) -> str:
        return zsh if argv[0] == "zsh" else bash

    return run


def test_the_owners_function_is_named_with_its_file_and_line(paths: Paths, tmp_path: Path) -> None:
    zshrc = write(
        tmp_path / ".zshrc",
        'export A=1\n\nsplash() {\n  command splash serve --port 9999 "${@:3}"\n}\n',
    )
    check = checks.shell_check(
        paths, tmp_path, runner_for(zsh=f"splash is a shell function from {zshrc}\n")
    )
    assert check.status == "fail"
    assert check.title == "`splash` is hidden by a shell function"
    assert check.detail == [
        f"{zshrc}:3 defines splash(), so your shell runs it instead of {paths.shim}."
    ]
    assert check.fix[0] == f"rename splash() at {zshrc}:3 to splash-legacy, then run: exec zsh"
    assert check.fix[1].startswith("Check: type -a splash")
    assert str(paths.shim) in check.fix[1]


def test_the_file_zsh_names_is_checked_before_the_startup_files(
    paths: Paths, tmp_path: Path
) -> None:
    functions = write(tmp_path / "zsh/functions.zsh", "\nfunction splash {\n  true\n}\n")
    write(tmp_path / ".zshrc", "# nothing about splash here\n")
    check = checks.shell_check(
        paths, tmp_path, runner_for(zsh=f"splash is a shell function from {functions}\n")
    )
    assert f"{functions}:2" in check.detail[0]


def test_a_function_in_a_startup_file_is_found_when_zsh_names_no_file(
    paths: Paths, tmp_path: Path
) -> None:
    zshrc = write(tmp_path / ".zshrc", "# a\n# b\nsplash () {\n}\n")
    check = checks.shell_check(paths, tmp_path, runner_for(zsh="splash is a function\n"))
    assert f"{zshrc}:3" in check.detail[0]


def test_an_alias_is_named_as_an_alias(paths: Paths, tmp_path: Path) -> None:
    zshrc = write(tmp_path / ".zshrc", "alias splash='command splash'\n")
    check = checks.shell_check(
        paths, tmp_path, runner_for(zsh="splash is an alias for command splash\n")
    )
    assert check.title == "`splash` is hidden by an alias"
    assert check.fix[0] == f"remove the alias at {zshrc}:1, then run: exec zsh"


def test_with_no_file_found_the_fix_still_says_what_to_rename(paths: Paths, tmp_path: Path) -> None:
    check = checks.shell_check(paths, tmp_path, runner_for(zsh="splash is a shell function\n"))
    assert check.status == "fail"
    assert "rename splash() to splash-legacy" in check.fix[0]


def test_a_brew_shellenv_that_puts_homebrew_first_is_named(paths: Paths, tmp_path: Path) -> None:
    zshrc = write(tmp_path / ".zshrc", 'eval "$(/opt/homebrew/bin/brew shellenv)"\n')
    output = f"splash is /opt/homebrew/bin/splash\nsplash is {paths.shim}\n"
    check = checks.shell_check(paths, tmp_path, runner_for(zsh=output))
    assert check.status == "fail"
    assert check.title == "`splash` runs another program first"
    assert f"{zshrc}:1" in check.fix[0]


def test_a_clean_shell_has_no_problem(paths: Paths, tmp_path: Path) -> None:
    check = checks.shell_check(paths, tmp_path, runner_for(zsh=f"splash is {paths.shim}\n"))
    assert check.status == "ok" and check.fix == []


def test_the_check_asks_login_interactive_shells(paths: Paths, tmp_path: Path) -> None:
    """A new Terminal tab is a login shell, and it reads ~/.zprofile as well as ~/.zshrc."""
    seen: list[list[str]] = []

    def run(argv: list[str]) -> str:
        seen.append(argv)
        return ""

    checks.shell_check(paths, tmp_path, run)
    assert seen and all(argv[1] == "-lic" for argv in seen)


@pytest.mark.skipif(
    shutil.which("zsh") is None or shutil.which("bash") is None, reason="needs zsh and bash"
)
def test_a_real_login_shell_with_brew_after_the_block_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end with the real bash: a `brew shellenv` appended to ~/.bash_profile after the
    block puts Homebrew first in a login shell, and the doctor says so."""
    home = tmp_path / "home"
    prefix = tmp_path / "brew"
    home.mkdir()
    brew_line = fake_brew(prefix)
    own = Paths(home / ".splash")
    shim.install_shim(own, ["/bin/true", "splash"])
    for name in shim.RC_FILES:
        shim.add_to_rc(home / name, own.bin_dir)
    with (home / ".bash_profile").open("a") as handle:
        handle.write(brew_line)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")

    check = checks.shell_check(own, home, checks.run_shell)
    assert check.status == "fail"
    assert check.title == "`splash` runs another program first"
    assert str(prefix / "bin/splash") in check.detail[0]
    assert "~/.bash_profile:" in check.fix[0], "the brew line is named where the user can find it"


def fake_brew(prefix: Path) -> str:
    """A Homebrew stand-in: `brew shellenv` prints the PATH export the real one prints."""
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    export = f'export PATH="{prefix}/bin:{prefix}/sbin${{PATH+:$PATH}}";'
    brew = bin_dir / "brew"
    brew.write_text(f"#!/bin/sh\n[ \"$1\" = shellenv ] && echo '{export}'\n")
    brew.chmod(0o755)
    engine = bin_dir / "splash"
    engine.write_text("#!/bin/sh\necho 'Splash 1.3.0'\n")
    engine.chmod(0o755)
    return f'eval "$({prefix}/bin/brew shellenv)"\n'

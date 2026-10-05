"""The `splash` CLI shim and its installation (SPEC §12, D21).

Two things are checked here: that the generated script does what a shim must
(run our entry point, exec so signals reach us, and be found by discovery so it
is skipped as an engine), and that the PATH block we add to the user's shell rc
files is marked, idempotent and exactly reversible.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from splash_gui.cli import install as shim
from splash_gui.cli.install import BEGIN, END


def test_the_generated_shim_execs_our_entry_point():
    script = shim.render(["/usr/bin/env", "uv", "run", "--project", "/src/manager", "splash"])
    text = script.decode()
    assert text.startswith("#!/bin/sh\n")
    assert text.count("\n") >= 2
    # exec, not a subshell: the CLI must receive signals and the exit status.
    assert 'exec /usr/bin/env uv run --project /src/manager splash "$@"' in text
    assert script.endswith(b'"$@"\n')


def test_the_shim_quotes_arguments_that_need_it():
    quoted = shim._shell_quote("/a path/with 'quotes'")
    assert quoted.startswith("'") and quoted.endswith("'")
    assert "'\\''" in quoted
    assert shim._shell_quote("/plain/path-1.2_3") == "/plain/path-1.2_3"


def test_installing_writes_an_owner_only_executable(paths):
    shim.install_shim(paths, ["/bin/true", "splash"])
    assert paths.shim.is_file()
    assert stat.S_IMODE(paths.shim.stat().st_mode) == shim.SHIM_MODE
    assert paths.shim.read_text().startswith("#!/bin/sh\n")
    assert shim.shim_is_ours(paths)


def test_installing_twice_is_idempotent(paths):
    first = shim.install_shim(paths, ["/bin/true", "splash"]).read_bytes()
    second = shim.install_shim(paths, ["/bin/true", "splash"]).read_bytes()
    assert first == second


def test_a_foreign_file_in_bin_is_not_mistaken_for_ours(paths):
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    paths.shim.write_text("#!/bin/sh\necho something else\n")
    assert not shim.shim_is_ours(paths)


def test_removing_reports_whether_there_was_anything(paths):
    assert shim.remove_shim(paths) is False
    shim.install_shim(paths, ["/bin/true", "splash"])
    assert shim.remove_shim(paths) is True
    assert not paths.shim.exists()


def test_the_path_block_is_added_once_and_inside_markers(tmp_path):
    rc = tmp_path / ".zprofile"
    rc.write_text("# my preamble\nexport EDITOR=vim\n")
    assert shim.add_to_rc(rc, Path("/home/x/.splash/bin")) is True
    text = rc.read_text()
    assert text.startswith("# my preamble\nexport EDITOR=vim\n")
    assert BEGIN in text and END in text
    assert 'export PATH="$HOME/.splash/bin:$PATH"' in text

    # Second time: already managed, so nothing changes.
    assert shim.add_to_rc(rc, Path("/home/x/.splash/bin")) is False
    assert rc.read_text() == text
    assert rc.read_text().count(BEGIN) == 1


def test_the_path_block_is_removed_exactly(tmp_path):
    rc = tmp_path / ".bash_profile"
    rc.write_text("# before\n")
    shim.add_to_rc(rc, Path("/home/x/.splash/bin"))
    shim.add_to_rc(rc, Path("/home/x/.splash/bin"))
    assert shim.remove_from_rc(rc) is True
    remaining = rc.read_text()
    assert BEGIN not in remaining and END not in remaining
    assert "splash" not in remaining
    assert remaining.startswith("# before\n"), "the user's own content survives"
    assert shim.remove_from_rc(rc) is False


def test_adding_to_a_missing_rc_file_creates_it(tmp_path):
    rc = tmp_path / ".zprofile"
    assert shim.add_to_rc(rc, Path("/home/x/.splash/bin")) is True
    assert BEGIN in rc.read_text()


def test_a_file_without_a_trailing_newline_is_handled(tmp_path):
    rc = tmp_path / ".zprofile"
    rc.write_text("export FOO=1")
    shim.add_to_rc(rc, Path("/home/x/.splash/bin"))
    text = rc.read_text()
    assert "export FOO=1\n" in text, "the existing line must not be joined to our block"
    assert BEGIN in text


def test_strip_block_leaves_unrelated_text_alone():
    text = "a\n" + BEGIN + "\nignored\n" + END + "\nb\n"
    stripped = shim.strip_block(text)
    assert "ignored" not in stripped
    assert stripped.startswith("a\n") and stripped.endswith("b\n")


def test_rc_report_lists_both_shells(tmp_path):
    (tmp_path / ".zprofile").write_text(BEGIN + "\nx\n" + END + "\n")
    report = {entry["file"]: entry for entry in shim.rc_report(tmp_path)}
    assert report[str(tmp_path / ".zprofile")]["managed"] is True
    assert report[str(tmp_path / ".bash_profile")]["present"] is False
    assert report[str(tmp_path / ".bash_profile")]["managed"] is False


def test_on_path_only_claims_the_first_entry(monkeypatch, tmp_path):
    first = tmp_path / "first"
    monkeypatch.setenv("PATH", os.pathsep.join([str(first), "/usr/bin"]))
    assert shim.on_path(first) is True
    assert shim.on_path(tmp_path / "second") is False


def test_self_test_lists_what_is_missing(paths, monkeypatch):
    problems = shim.self_test(paths)
    assert any("not installed" in problem for problem in problems)
    shim.install_shim(paths, ["/bin/true", "splash"])
    problems = shim.self_test(paths)
    assert not any("not installed" in problem for problem in problems)
    assert not any("not executable" in problem for problem in problems)


def test_the_shim_is_skipped_when_discovering_the_engine(paths, tmp_path):
    """D21: our shim must never be mistaken for the engine, or we exec ourselves."""
    import subprocess

    from splash_gui.engine.discovery import discover

    shim.install_shim(paths, ["/bin/true", "splash"])
    real = tmp_path / "splash"
    real.write_text("#!/bin/sh\necho 'Splash 1.2.0'\n")
    real.chmod(0o755)

    def runner(argv, timeout):
        return subprocess.CompletedProcess(list(argv), 0, "Splash 1.2.0\n", "")

    # Only the shim is on PATH: discovery must skip it and report not found.
    only_shim = discover(
        env={"PATH": str(paths.bin_dir)},
        shim_paths=(paths.shim,),
        prefix=None,
        runner=runner,
    )
    assert only_shim.found is False
    assert only_shim.cli is None

    both = discover(
        env={"PATH": os.pathsep.join([str(paths.bin_dir), str(tmp_path)])},
        shim_paths=(paths.shim,),
        prefix=None,
        runner=runner,
    )
    assert both.cli == real
    assert both.version == "1.2.0"


def test_a_shim_without_the_marker_is_still_skipped_by_path(paths, tmp_path):
    """Skipping is by resolved path as well as by content, so a rewritten shim
    still cannot make the manager exec itself."""
    import subprocess

    from splash_gui.engine.discovery import discover

    shim.install_shim(paths, ["/bin/true", "splash"])
    paths.shim.write_text("#!/bin/sh\n# rewritten by hand\nexec true\n")

    def runner(argv, timeout):
        return subprocess.CompletedProcess(list(argv), 0, "Splash 1.2.0\n", "")

    result = discover(
        env={"PATH": str(paths.bin_dir)},
        shim_paths=(paths.shim,),
        prefix=None,
        runner=runner,
    )
    assert result.found is False


def test_the_shim_runs_and_reports_a_missing_manager(tmp_path):
    """Executing the script must fail loudly, not silently, when it cannot start."""
    script = tmp_path / "splash"
    script.write_bytes(shim.render([str(tmp_path / "definitely-not-here"), "splash"]))
    script.chmod(0o755)
    result = subprocess.run([str(script), "status"], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert result.stderr.strip(), "a broken shim must say why on stderr"


def test_the_manager_installs_the_shim_on_startup(client, paths):
    """SPEC §12.1: the app installs it, so `splash` works without the UI."""
    status = client.get("/api/admin/cli/shim")
    assert status.status_code == 200, status.text
    body = dict(status.json())
    assert body["installed"] is True
    assert body["executable"] is True
    assert paths.shim.is_file()
    assert body["command"].endswith("splash")


def test_the_api_reports_and_repairs_the_shim(client, paths):
    assert client.get("/api/admin/cli/shim").json()["installed"] is True
    paths.shim.unlink()
    assert client.get("/api/admin/cli/shim").json()["installed"] is False

    installed = client.post("/api/admin/cli/shim", json={})
    assert installed.status_code == 200, installed.text
    assert installed.json()["installed"] is True
    assert paths.shim.is_file()


def test_deleting_the_shim_removes_it_and_the_path_block(client, paths, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    rc = home / ".zprofile"
    rc.write_text("# user content\n")
    client.app.state.manager.user_home = home

    client.post("/api/admin/cli/shim", json={"add_to_path": True})
    assert BEGIN in rc.read_text()
    assert client.delete("/api/admin/cli/shim").status_code == 204
    assert not paths.shim.exists()
    assert BEGIN not in rc.read_text()
    assert "# user content" in rc.read_text()


def test_deleting_a_shim_that_is_not_there_is_a_404(client, paths):
    paths.shim.unlink(missing_ok=True)
    response = client.delete("/api/admin/cli/shim")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "shim_not_installed"


def test_the_entry_point_is_importable_as_a_console_script():
    """pyproject declares `splash = splash_gui.cli:main`; the shim depends on it."""
    from splash_gui.cli import COMMANDS, main, parser

    assert "launch" in COMMANDS and "doctor" in COMMANDS
    assert callable(main)
    assert parser().prog == "splash"


def test_main_reports_an_unknown_command_by_passing_it_through(monkeypatch):
    """`splash serve ...` and anything unknown are exec'd to the real engine."""
    from splash_gui import cli as cli_module

    calls: list[list[str]] = []

    def fake_discover(*, shim_paths=(), **kwargs):
        from splash_gui.engine.discovery import EngineInfo

        return EngineInfo(found=True, cli=Path("/opt/homebrew/opt/splash/bin/splash"))

    def fake_execv(path, argv):
        calls.append([str(path), *argv])

    class FakeOs:
        """Just enough of the os module for main()'s exec passthrough."""

        environ = os.environ

        execv = staticmethod(fake_execv)

    monkeypatch.setattr(cli_module, "discover", fake_discover)
    monkeypatch.setattr(cli_module, "os", FakeOs())
    # The stubbed execv returns instead of replacing the process, so main goes
    # on to parse the arguments and argparse rejects the engine's own command.
    with pytest.raises(SystemExit):
        cli_module.main(["serve", "--model", "x"])
    # execv's argv[0] is the program, as the real engine expects.
    engine = "/opt/homebrew/opt/splash/bin/splash"
    assert calls == [[engine, engine, "serve", "--model", "x"]]


def test_main_reports_127_when_the_engine_is_missing(monkeypatch, capsys):
    from splash_gui import cli as cli_module
    from splash_gui.engine.discovery import EngineInfo

    monkeypatch.setattr(
        cli_module, "discover", lambda **kwargs: EngineInfo(found=False, error="no brew")
    )
    assert cli_module.main(["serve"]) == 127
    assert "no brew" in capsys.readouterr().err


def test_main_errors_are_reported_without_a_traceback(monkeypatch, capsys):
    """`splash status` with no manager is a one-line message, not a stack trace."""
    from splash_gui import cli as cli_module

    class Down:
        """A manager that is not running: `running()` is False, `http` is a client."""

        def __init__(self, *args, **kwargs):
            self.http = type("Http", (), {"close": lambda self: None})()

        def running(self) -> bool:
            return False

    monkeypatch.setattr(cli_module, "Client", Down)
    # docs/ui/11 §4.1, §13.2: the manager being down is exit 3.
    assert cli_module.main(["status"]) == 3
    out, err = capsys.readouterr()
    assert out == "Manager    not running · start with: splash start\n"
    assert "Traceback" not in err
    assert cli_module.main(["ls"]) == 3
    err = capsys.readouterr().err
    assert "Splash GUI is not running" in err and "splash start" in err
    assert "Traceback" not in err


def test_the_shim_path_is_not_written_without_being_asked(app, tmp_path, monkeypatch):
    """The PATH block is opt-in: starting the manager, reading the status and
    repairing the shim never edit rc files; only `add_to_path: true` does."""
    from fastapi.testclient import TestClient

    home = tmp_path / "rc-home"
    home.mkdir()
    zprofile = home / ".zprofile"
    zprofile.write_bytes(b"# mine\n")
    app.state.manager.user_home = home
    calls: list[Path] = []
    real_add = shim.add_to_rc

    def spy(path: Path, bin_dir: Path) -> bool:
        calls.append(path)
        return real_add(path, bin_dir)

    monkeypatch.setattr(shim, "add_to_rc", spy)
    token = app.state.manager.auth.cli_token()
    with TestClient(
        app, client=("127.0.0.1", 50000), headers={"Authorization": f"Bearer {token}"}
    ) as client:
        assert client.get("/api/admin/cli/shim").status_code == 200
        assert client.post("/api/admin/cli/shim", json={}).status_code == 200
        assert client.post("/api/admin/cli/shim").status_code == 200
        assert calls == []
        assert zprofile.read_bytes() == b"# mine\n"
        assert not (home / ".bash_profile").exists()
        # The same home is the one an explicit request edits, so the checks
        # above looked at the right files.
        assert client.post("/api/admin/cli/shim", json={"add_to_path": True}).status_code == 200
    assert BEGIN in zprofile.read_text()
    assert {p.name for p in calls} == {".zprofile", ".bash_profile"}


def test_the_generated_shim_carries_the_discovery_marker(paths, tmp_path):
    """discovery.SHIM_MARKER is the content check that skips a copy of the shim
    (another SPLASH_GUI_HOME, a copy in /usr/local/bin); without it the
    passthrough would exec itself forever."""
    import subprocess

    from splash_gui.engine.discovery import SHIM_MARKER, discover

    assert SHIM_MARKER in shim.render(["/bin/true", "splash"]).decode()
    copy_dir = tmp_path / "elsewhere"
    copy_dir.mkdir()
    copy = copy_dir / "splash"
    copy.write_bytes(shim.render(["/bin/true", "splash"]))
    copy.chmod(0o755)

    def runner(argv, timeout):
        return subprocess.CompletedProcess(list(argv), 0, "Splash 1.2.0\n", "")

    result = discover(env={"PATH": str(copy_dir)}, shim_paths=(), prefix=None, runner=runner)
    assert result.found is False


RC_ORIGINALS = {
    "missing": None,
    "empty": b"",
    "trailing newline": b"export A=1\n",
    "no trailing newline": b"export A=1",
    "blank lines around": b"\n\n# top\nexport A=1\n\n\n",
    "crlf": b"export A=1\r\nexport B=2\r\n",
    "latin-1 bytes": b"# caf\xe9\nexport A=1\n",
    "only a newline": b"\n",
}


@pytest.mark.parametrize("original", list(RC_ORIGINALS.values()), ids=list(RC_ORIGINALS))
def test_rc_edits_are_reversed_byte_for_byte(tmp_path, original):
    rc = tmp_path / ".zprofile"
    if original is not None:
        rc.write_bytes(original)
        rc.chmod(0o600)
    assert shim.add_to_rc(rc, tmp_path / "bin") is True
    assert shim.add_to_rc(rc, tmp_path / "bin") is False, "a repeated add changes nothing"
    added = rc.read_bytes()
    assert added.count(BEGIN.encode()) == 1
    if original:
        assert added.startswith(original.rstrip(b"\n")), "the user's bytes stay first"
        assert stat.S_IMODE(rc.stat().st_mode) == 0o600, "the file's mode is kept"
    assert shim.remove_from_rc(rc) is True
    if original is None:
        assert not rc.exists(), "a file we created is removed again"
    else:
        assert rc.read_bytes() == original
        assert stat.S_IMODE(rc.stat().st_mode) == 0o600
    assert shim.remove_from_rc(rc) is False


def test_a_non_utf8_rc_file_is_never_clobbered(tmp_path):
    rc = tmp_path / ".bash_profile"
    original = b"# r\xe9sum\xe9\nexport PATH=/opt/x:$PATH\n"
    rc.write_bytes(original)
    shim.add_to_rc(rc, tmp_path / "bin")
    assert rc.read_bytes().startswith(original)


def test_a_symlinked_rc_file_is_edited_through_its_link(tmp_path):
    dotfiles = tmp_path / "dotfiles" / "zprofile"
    dotfiles.parent.mkdir()
    dotfiles.write_bytes(b"export A=1\n")
    rc = tmp_path / ".zprofile"
    rc.symlink_to(dotfiles)
    shim.add_to_rc(rc, tmp_path / "bin")
    assert rc.is_symlink() and BEGIN in dotfiles.read_text()
    shim.remove_from_rc(rc)
    assert rc.is_symlink() and dotfiles.read_bytes() == b"export A=1\n"


def test_a_broken_rc_symlink_is_left_alone(tmp_path):
    rc = tmp_path / ".zprofile"
    rc.symlink_to(tmp_path / "missing" / "zprofile")
    with pytest.raises(OSError, match="broken symlink"):
        shim.add_to_rc(rc, tmp_path / "bin")
    assert rc.is_symlink() and not (tmp_path / "missing").exists()


def test_content_added_after_our_block_survives_removal(tmp_path):
    rc = tmp_path / ".zprofile"
    shim.add_to_rc(rc, tmp_path / "bin")
    with rc.open("a") as handle:
        handle.write('eval "$(/opt/homebrew/bin/brew shellenv)"\n')
    shim.remove_from_rc(rc)
    assert rc.read_text() == 'eval "$(/opt/homebrew/bin/brew shellenv)"\n'

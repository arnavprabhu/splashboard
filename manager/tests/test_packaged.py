"""Bundle detection and the packaged form of the CLI shim and web dist (PKG-3).

The fake bundle only has the layout that matters: an interpreter path and a
web folder. Nothing here runs a real bundle; tests/ for that are the lanes in
docs/plans/packaging.md PKG-3."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from splash_gui import app as app_module
from splash_gui import packaged
from splash_gui.cli import install as shim

FAKE_APP = Path("/Applications/Test.app")
FAKE_PYTHON = FAKE_APP / "Contents/Resources/manager/python/bin/python3"


def _run_as(monkeypatch: pytest.MonkeyPatch, prefix: Path) -> None:
    """Make this interpreter look like it runs from `prefix` (sys.prefix is what the
    packaged code reads; see packaged.this_interpreter)."""
    monkeypatch.setattr(sys, "prefix", str(prefix))


def test_bundle_root_is_the_app_folder_for_the_bundled_interpreter() -> None:
    assert packaged.bundle_root(str(FAKE_PYTHON)) == FAKE_APP


def test_bundle_root_keeps_a_moved_bundle_location() -> None:
    moved = Path(
        "/Users/someone/Desktop/Moved Copy/Test.app/Contents/Resources/manager/python/bin/python3"
    )
    assert packaged.bundle_root(str(moved)) == moved.parents[5]
    assert moved.parents[5].name == "Test.app"


@pytest.mark.parametrize(
    "executable",
    [
        "/repo/manager/.venv/bin/python3",
        "/opt/homebrew/bin/python3",
        "/Applications/Test.app/Contents/MacOS/SplashGUI",
        "/Applications/Test.app/Contents/Resources/manager/bin/python3",
        "python3",
    ],
)
def test_bundle_root_is_none_outside_the_bundled_runtime(executable: str) -> None:
    assert packaged.bundle_root(executable) is None


def test_bundle_root_defaults_to_this_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    _run_as(monkeypatch, FAKE_PYTHON.parents[1])
    assert packaged.bundle_root() == FAKE_APP
    assert packaged.bundled_python(FAKE_APP) == FAKE_PYTHON
    assert packaged.this_interpreter() == FAKE_PYTHON


def test_bundle_root_survives_a_relative_launchd_argv0(monkeypatch: pytest.MonkeyPatch) -> None:
    # launchd starts the agent with the relative BundleProgram argv[0] and cwd "/", so
    # sys.executable reads "//Contents/..." (reproduced in PKG-4). sys.prefix stays absolute.
    monkeypatch.setattr(sys, "executable", "//Contents/Resources/manager/python/bin/python3")
    _run_as(monkeypatch, FAKE_PYTHON.parents[1])
    assert packaged.bundle_root() == FAKE_APP
    assert shim.interpreter_command() == [str(FAKE_PYTHON), "-I", "-B", "-m", "splash_gui.cli"]


def test_source_tree_shim_command_is_uv_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _run_as(monkeypatch, Path("/repo/manager/.venv"))
    command = shim.interpreter_command()
    assert command[1:] == ["run", "--project", command[3], "splash"]
    assert command[3].endswith("manager")
    assert "-I" not in command


def test_packaged_shim_command_runs_the_bundled_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_as(monkeypatch, FAKE_PYTHON.parents[1])
    command = shim.interpreter_command()
    assert command == [str(FAKE_PYTHON), "-I", "-B", "-m", "splash_gui.cli"]
    script = shim.render(command).decode()
    assert f'exec {FAKE_PYTHON} -I -B -m splash_gui.cli "$@"' in script


def test_packaged_shim_follows_the_app_when_it_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    _run_as(monkeypatch, FAKE_PYTHON.parents[1])
    first = shim.render(shim.interpreter_command())
    moved = Path("/Applications/Moved/Test.app/Contents/Resources/manager/python/bin/python3")
    _run_as(monkeypatch, moved.parents[1])
    assert shim.render(shim.interpreter_command()) != first
    assert str(moved).encode() in shim.render(shim.interpreter_command())


def test_web_dist_order_is_env_then_bundle_then_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_dist = Path(app_module.__file__).resolve().parents[2] / "web" / "dist"
    monkeypatch.delenv(app_module.WEB_DIST_ENV, raising=False)
    _run_as(monkeypatch, Path("/repo/manager/.venv"))
    assert app_module.default_web_dist() == repo_dist

    _run_as(monkeypatch, FAKE_PYTHON.parents[1])
    assert app_module.default_web_dist() == FAKE_APP / "Contents/Resources/web"

    override = tmp_path / "override"
    monkeypatch.setenv(app_module.WEB_DIST_ENV, str(override))
    assert app_module.default_web_dist() == override

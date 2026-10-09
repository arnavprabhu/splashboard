"""Engine discovery with fake PATH and brew (SPEC §6.1)."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.engine import discovery as d
from splash_gui.engine import serve_options
from splash_gui.engine.serve_options import (
    EngineOptions,
    EngineOptionsCache,
    parse_helper_output,
    run_helper,
)

from .conftest import HAVE_SPLASH, LOOPBACK_CLIENT, SPLASH_PKG, write_script


def fake_pkg(root: Path, version: str = "1.3.0", *, release: bool = True) -> Path:
    """A Homebrew-shaped keg: <root>/bin/splash wrapper + <root>/libexec package."""
    pkg = root / "libexec"
    (pkg / "server").mkdir(parents=True)
    (pkg / "install").mkdir(parents=True)
    (pkg / "install" / "launcher.py").write_text("")
    write_script(pkg / "python" / "bin" / "python3", "exit 0\n")
    if release:
        (pkg / "release.json").write_text(f'{{"version": "{version}"}}')
    write_script(
        root / "bin" / "splash",
        f'export PYTHONDONTWRITEBYTECODE=1\nexec "{pkg}/python/bin/python3" -u '
        f'"{pkg}/install/launcher.py" "$@"\necho "Splash {version}"\n',
    )
    return pkg


def runner_for(versions: dict[str, str], prefix: Path | None = None) -> d.Runner:
    def run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv[-1] == "--prefix":
            out = f"{prefix}\n" if prefix else ""
            return subprocess.CompletedProcess(list(argv), 0 if prefix else 1, out, "")
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(list(argv), 0, versions.get(argv[0], ""), "")
        raise AssertionError(argv)

    return run


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Splash 1.2.0", (1, 2, 0)),
        ("Splash 1.10.3\n", (1, 10, 3)),
        ("Splash (source checkout)", None),
        ("", None),
    ],
)
def test_parse_version(text: str, expected: tuple[int, int, int] | None) -> None:
    assert d.parse_version(text) == expected


@pytest.mark.parametrize(
    ("version", "support"),
    [
        ((1, 3, 0), "supported"),
        ((1, 3, 9), "supported"),
        ((1, 4, 0), "untested"),
        ((2, 0, 0), "untested"),
        ((1, 2, 1), "too_old"),
        ((1, 2, 0), "too_old"),
        (None, "unknown"),
    ],
)
def test_supported_range(version: tuple[int, int, int] | None, support: str) -> None:
    assert d.support_for(version) == support


def test_brew_location_is_preferred(tmp_path: Path) -> None:
    prefix = tmp_path / "homebrew"
    keg = prefix / "opt" / "splash"
    pkg = fake_pkg(keg)
    other = fake_pkg(tmp_path / "elsewhere")
    cli = keg / "bin" / "splash"
    info = d.discover(
        env={"PATH": str(other.parent / "bin")},
        runner=runner_for({str(cli): "Splash 1.3.0"}),
        prefix=prefix,
    )
    assert info.found and info.source == "brew" and info.cli == cli
    assert info.version == "1.3.0" and info.support == "supported" and info.banner is None
    assert info.pkg == pkg.resolve() or info.pkg == pkg
    assert info.python == info.pkg / "python" / "bin" / "python3"
    assert info.install_dir is not None and info.server_dir is not None
    assert info.error is None


def test_setting_overrides_everything(tmp_path: Path) -> None:
    custom = fake_pkg(tmp_path / "custom", "1.4.1")
    cli = custom.parent / "bin" / "splash"
    info = d.discover(
        str(cli),
        env={"PATH": ""},
        runner=runner_for({str(cli): "Splash 1.4.1"}),
        prefix=tmp_path / "nothing",
    )
    assert info.source == "setting" and info.support == "untested"
    assert info.banner == "GUI untested with Splash 1.4"


def test_env_override(tmp_path: Path) -> None:
    custom = fake_pkg(tmp_path / "custom")
    cli = custom.parent / "bin" / "splash"
    info = d.discover(
        env={"PATH": "", d.REAL_SPLASH_ENV: str(cli)},
        runner=runner_for({str(cli): "Splash 1.3.0"}),
        prefix=None,
    )
    assert info.source == "env"


def test_path_lookup_skips_our_shim(tmp_path: Path) -> None:
    shim_dir = tmp_path / "home" / ".splash" / "bin"
    shim = write_script(shim_dir / "splash", f"# {d.SHIM_MARKER}\nexec python -m splash_gui\n")
    marked_dir = tmp_path / "marked"
    write_script(marked_dir / "splash", f"# {d.SHIM_MARKER} copy\n")
    real = fake_pkg(tmp_path / "real")
    real_cli = real.parent / "bin" / "splash"
    info = d.discover(
        env={"PATH": f"{shim_dir}:{marked_dir}:{real_cli.parent}"},
        shim_paths=(shim,),
        runner=runner_for({str(real_cli): "Splash 1.3.0"}),
        prefix=None,
    )
    assert info.found and info.source == "path" and info.cli == real_cli


def test_setting_pointing_at_shim_is_skipped(tmp_path: Path) -> None:
    shim = write_script(tmp_path / "bin" / "splash", f"# {d.SHIM_MARKER}\n")
    info = d.discover(
        str(shim), env={"PATH": ""}, shim_paths=(shim,), runner=runner_for({}), prefix=None
    )
    assert not info.found
    assert any("shim" in note for note in info.notes)


def test_not_installed(tmp_path: Path) -> None:
    info = d.discover(env={"PATH": str(tmp_path)}, runner=runner_for({}), prefix=None)
    assert not info.found
    assert info.error is not None and "brew install incoai/tap/splash" in info.error


def test_bad_setting_is_reported_and_skipped(tmp_path: Path) -> None:
    real = fake_pkg(tmp_path / "real")
    cli = real.parent / "bin" / "splash"
    info = d.discover(
        str(tmp_path / "missing"),
        env={"PATH": str(cli.parent)},
        runner=runner_for({str(cli): "Splash 1.3.0"}),
        prefix=None,
    )
    assert info.source == "path"
    assert any("not an executable" in note for note in info.notes)


def test_version_falls_back_to_release_json(tmp_path: Path) -> None:
    real = fake_pkg(tmp_path / "real", "1.3.3")
    cli = real.parent / "bin" / "splash"
    info = d.discover(env={"PATH": str(cli.parent)}, runner=runner_for({}), prefix=None)
    assert info.version == "1.3.3"


def test_source_checkout(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    (root / "server").mkdir(parents=True)
    (root / "install").mkdir()
    (root / "install" / "launcher.py").write_text("")
    write_script(root / ".venv" / "bin" / "python", "exit 0\n")
    cli = write_script(root / "splash", 'exec "$ROOT/install/launcher.py"\n')
    info = d.discover(
        str(cli),
        env={"PATH": ""},
        runner=runner_for({str(cli): "Splash (source checkout)"}),
        prefix=None,
    )
    assert info.source_checkout and info.pkg == root.resolve()
    assert info.python == root.resolve() / ".venv" / "bin" / "python"
    assert info.version is None and info.support == "unknown"


def test_brew_prefix_runner_failure() -> None:
    def boom(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(argv), timeout)

    assert d.brew_prefix(boom, brew="/usr/bin/true") is None


@pytest.mark.skipif(not HAVE_SPLASH, reason="Splash 1.3.0 is not installed via Homebrew")
def test_real_homebrew_engine() -> None:
    info = d.discover(env={"PATH": "/usr/bin:/bin"})
    assert info.found and info.source == "brew"
    assert info.version is not None and info.version.startswith("1.3.")
    assert info.pkg is not None and info.pkg.resolve() == SPLASH_PKG.resolve()
    assert info.python is not None and info.python.exists()


# serve_options helper (SPEC §8.4) -----------------------------------------------------


def test_parse_helper_output_marks_unknown() -> None:
    options = parse_helper_output(
        '{"version": "1.4.0", "errors": [], "options": ['
        '{"flag": "--max-context", "dest": "max_context", "default": null, "help": "ctx",'
        ' "action": "store", "source": "shared"},'
        '{"flag": "--shiny-new", "dest": "shiny_new", "default": false, "help": "new!",'
        ' "action": "store_true", "source": "shared"}]}'
    )
    assert options.available and options.version == "1.4.0"
    assert [o.flag for o in options.unknown()] == ["--shiny-new"]
    assert options.by_flag()["--shiny-new"].takes_value is False


def test_helper_failures_are_reported(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    sleeper = write_script(tmp_path / "python3", "sleep 5\n")
    info = d.EngineInfo(found=True, cli=sleeper, pkg=pkg, python=sleeper)
    result = run_helper(info, timeout=0.2)
    assert not result.available and "timed out" in result.errors[0]
    failing = write_script(tmp_path / "py-fail", "echo boom >&2; exit 3\n")
    result = run_helper(d.EngineInfo(found=True, cli=failing, pkg=pkg, python=failing))
    assert not result.available and "boom" in result.errors[0]
    assert not run_helper(d.EngineInfo(found=False, error="nope")).available


@pytest.mark.skipif(not HAVE_SPLASH, reason="Splash 1.3.0 is not installed via Homebrew")
def test_helper_against_real_splash() -> None:
    info = d.discover(env={"PATH": "/usr/bin:/bin"})
    options = EngineOptionsCache().get(info)
    assert options.available, options.errors
    assert options.errors == ()
    flags = options.by_flag()
    for flag in (
        "--max-context",
        "--kv-format",
        "--persistent-cache",
        "--queue-size",
        "--port",
        "--model",
        "--revision",
        "--draft-model",
        "--language-only",
        "--offline",
        "--no-webui",
        "--api-key",
    ):
        assert flag in flags, flag
    assert flags["--kv-format"].choices == ["int8", "bf16"]
    assert flags["--queue-size"].default == 32
    assert flags["--api-key"].secret and flags["--api-key"].environment == "SPLASH_API_KEY"
    assert options.unknown() == []  # Splash 1.3.0 has no option Appendix A doesn't map


def test_concurrent_reads_run_the_helper_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The startup read and a first Settings load share one helper run (SPEC §8.4)."""
    calls: list[d.EngineInfo] = []
    started, release = threading.Event(), threading.Event()

    def slow_helper(engine: d.EngineInfo, timeout: float = 20) -> EngineOptions:
        calls.append(engine)
        started.set()
        release.wait(5)
        return EngineOptions(available=True, version="1.3.0")

    monkeypatch.setattr(serve_options, "run_helper", slow_helper)
    cache = EngineOptionsCache()
    info = d.EngineInfo(found=True, cli=Path("/opt/splash/bin/splash"), version="1.3.0")
    results: list[EngineOptions] = []
    readers = [threading.Thread(target=lambda: results.append(cache.get(info))) for _ in range(3)]
    readers[0].start()
    assert started.wait(5)
    for reader in readers[1:]:
        reader.start()
    time.sleep(0.1)  # let the other readers reach the cache while the helper runs
    release.set()
    for reader in readers:
        reader.join(5)
    assert len(calls) == 1
    assert len(results) == 3 and all(result is results[0] for result in results)


def test_the_serve_options_are_read_at_startup(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC §8.4: the manager reads the helper as it starts, so Settings does not wait."""
    started = threading.Event()
    calls: list[d.EngineInfo] = []

    def helper(engine: d.EngineInfo, timeout: float = 20) -> EngineOptions:
        calls.append(engine)
        started.set()
        return EngineOptions(available=False, errors=("stub helper",))

    monkeypatch.setattr(serve_options, "run_helper", helper)
    token = app.state.manager.auth.cli_token()
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, client=LOOPBACK_CLIENT, headers=headers) as client:
        assert started.wait(10), "the helper did not run at startup"
        body = client.get("/api/admin/settings/schema").json()
    assert [call.version for call in calls] == ["1.3.0"]  # read once; Settings reuses it
    assert body["engine_options"]["errors"] == ["stub helper"]

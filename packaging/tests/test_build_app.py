"""Tests for packaging/scripts/build-app.sh: the --out guard, the verify home guard and the inputs checked before
the first build step (docs/plans/packaging.md, PKG-5 review).

Each test runs a copy of the script in a skeleton repo under pytest's tmp_path. A regression can therefore only
touch that folder, never the real build/ or any app outside it. Run from the repo root:
uv run --no-project --with pytest pytest packaging/tests
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "packaging" / "scripts" / "build-app.sh"
TIMEOUT = 120

# Everything build-app.sh reads or copies, relative to the repo root.
INPUTS = [
    "packaging/launchagent.plist.in",
    "packaging/identity.env",
    "packaging/scripts/collect-licenses.py",
    "web/src/assets/fonts/archivo-latin.woff2",
    "web/public/licenses/Archivo-OFL.txt",
    "LICENSE",
    "NOTICE",
    "packaging/scripts/sign.sh",
    "packaging/entitlements/app.plist",
    "packaging/entitlements/python.plist",
    "packaging/entitlements/python-adhoc.plist",
    "packaging/scripts/lib-sparkle.sh",
    "packaging/entitlements/app-adhoc.plist",
]


def skeleton(root: Path, omit: str | None = None) -> Path:
    """A repo root holding the script and every input except `omit`. identity.env is real (it is sourced)."""
    (root / "packaging" / "scripts").mkdir(parents=True)
    script = root / "packaging" / "scripts" / "build-app.sh"
    shutil.copy2(SCRIPT, script)
    for rel in INPUTS:
        if rel == omit:
            continue
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if rel == "packaging/identity.env":
            shutil.copy2(REPO / rel, dest)
        else:
            dest.write_text("placeholder\n", encoding="utf-8")
    return script


def run(
    script: Path, *args: str, verify_home: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Runs the script. SPLASH_GUI_VERIFY_HOME is set only when verify_home is given, never inherited."""
    env = {k: v for k, v in os.environ.items() if k != "SPLASH_GUI_VERIFY_HOME"}
    if verify_home is not None:
        env["SPLASH_GUI_VERIFY_HOME"] = verify_home
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
        env=env,
    )


def sentinel_app(folder: Path) -> Path:
    """A fake .app with one file in it. The test passes it to --out and checks the file survives."""
    app = folder / "Victim.app"
    app.mkdir(parents=True)
    keep = app / "keep.txt"
    keep.write_text("keep\n", encoding="utf-8")
    return keep


def test_out_outside_build_is_refused(tmp_path: Path) -> None:
    script = skeleton(tmp_path / "repo")
    keep = sentinel_app(tmp_path / "outside")
    result = run(script, "--out", str(keep.parent))
    assert result.returncode == 1
    assert "must be a .app under" in result.stderr
    assert keep.read_text(encoding="utf-8") == "keep\n"
    assert not (tmp_path / "repo" / "build").exists()


def test_out_through_a_symlink_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    script = skeleton(root)
    keep = sentinel_app(tmp_path / "outside")
    (root / "build").mkdir()
    (root / "build" / "escape").symlink_to(
        tmp_path / "outside", target_is_directory=True
    )
    result = run(script, "--out", str(root / "build" / "escape" / "Victim.app"))
    assert result.returncode == 1
    assert "must be a .app under" in result.stderr
    assert keep.read_text(encoding="utf-8") == "keep\n"


def test_out_with_parent_traversal_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    script = skeleton(root)
    keep = sentinel_app(tmp_path / "outside")
    result = run(script, "--out", str(root / "build" / ".." / "outside" / "Victim.app"))
    assert result.returncode == 1
    assert "must not contain .." in result.stderr
    assert keep.read_text(encoding="utf-8") == "keep\n"


def test_out_under_build_passes_the_guard(tmp_path: Path) -> None:
    # Control: with every input present the script gets past the guard and the input checks, into make web.
    # The skeleton's web/ folder would make `make web` a no-op, so its Makefile's web target fails on purpose.
    script = skeleton(tmp_path / "repo")
    (tmp_path / "repo" / "Makefile").write_text(
        ".PHONY: web\nweb:\n\tfalse\n", encoding="utf-8"
    )
    result = run(
        script, "--out", str(tmp_path / "repo" / "build" / "package" / "Splashboard.app")
    )
    assert result.returncode == 1
    assert "make web failed" in result.stderr
    assert "must be" not in result.stderr
    assert "missing" not in result.stderr


def test_default_variant_under_build_verify_is_refused(tmp_path: Path) -> None:
    # The default variant has the owner's bundle id and agent label, so it may not be written under build/verify/.
    root = tmp_path / "repo"
    script = skeleton(root)
    out = root / "build" / "verify" / "run-1" / "Splashboard.app"
    result = run(script, "--out", str(out))
    assert result.returncode == 1
    assert "--variant default may not write under" in result.stderr
    assert not out.parent.exists()


# The verify home is baked into the bundle's LSEnvironment and the agent plist, so the .verify app reads and
# writes it. It must be a throwaway folder under build/verify/, as package.sh requires. The owner's ~/.splash
# is never passed here: the tests use tmp_path folders that stand in for it.


def test_verify_home_outside_build_verify_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    script = skeleton(root)
    home = tmp_path / "owner-home"
    result = run(
        script,
        "--variant",
        "verify",
        "--out",
        str(root / "build" / "package" / "Splashboard.app"),
        verify_home=str(home),
    )
    assert result.returncode == 1
    assert "must be under" in result.stderr
    assert "make web failed" not in result.stderr
    assert not home.exists()


def test_verify_home_with_parent_traversal_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    script = skeleton(root)
    result = run(
        script,
        "--variant",
        "verify",
        "--out",
        str(root / "build" / "package" / "Splashboard.app"),
        verify_home=str(root / "build" / "verify" / ".." / ".." / "outside-home"),
    )
    assert result.returncode == 1
    assert "must not contain .." in result.stderr
    assert "make web failed" not in result.stderr
    assert not (root / "outside-home").exists()


def test_verify_home_through_a_symlink_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    script = skeleton(root)
    target = tmp_path / "owner-home-target"
    target.mkdir()
    (root / "build" / "verify").mkdir(parents=True)
    (root / "build" / "verify" / "escape").symlink_to(target, target_is_directory=True)
    result = run(
        script,
        "--variant",
        "verify",
        "--out",
        str(root / "build" / "package" / "Splashboard.app"),
        verify_home=str(root / "build" / "verify" / "escape" / "home"),
    )
    assert result.returncode == 1
    assert "must be under" in result.stderr
    assert "make web failed" not in result.stderr
    assert not (target / "home").exists()


def test_verify_home_through_a_dangling_symlink_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    script = skeleton(root)
    missing = tmp_path / "missing-target"
    (root / "build" / "verify").mkdir(parents=True)
    (root / "build" / "verify" / "dangle").symlink_to(missing, target_is_directory=True)
    result = run(
        script,
        "--variant",
        "verify",
        "--out",
        str(root / "build" / "package" / "Splashboard.app"),
        verify_home=str(root / "build" / "verify" / "dangle" / "home"),
    )
    assert result.returncode == 1
    assert "must not pass through a symlink" in result.stderr
    assert "make web failed" not in result.stderr
    assert not missing.exists()


def test_verify_home_under_build_verify_passes_the_guard(tmp_path: Path) -> None:
    # Control: a throwaway home under build/verify/ gets past the guard into make web, which fails on purpose
    # (the skeleton's Makefile). The home is not created before the build step.
    root = tmp_path / "repo"
    script = skeleton(root)
    (root / "Makefile").write_text(".PHONY: web\nweb:\n\tfalse\n", encoding="utf-8")
    home = root / "build" / "verify" / "run-1" / "home"
    result = run(
        script,
        "--variant",
        "verify",
        "--out",
        str(root / "build" / "package" / "Splashboard.app"),
        verify_home=str(home),
    )
    assert result.returncode == 1
    assert "make web failed" in result.stderr
    assert "must be" not in result.stderr
    assert "must not" not in result.stderr
    assert not home.exists()


@pytest.mark.parametrize("omit", INPUTS)
def test_missing_input_stops_before_the_build(tmp_path: Path, omit: str) -> None:
    root = tmp_path / "repo"
    script = skeleton(root, omit=omit)
    result = run(script, "--out", str(root / "build" / "package" / "Splashboard.app"))
    assert result.returncode == 1
    assert "missing" in result.stderr
    assert Path(omit).name in result.stderr
    assert "make web failed" not in result.stderr
    assert not (root / "build" / "package-web.log").exists()

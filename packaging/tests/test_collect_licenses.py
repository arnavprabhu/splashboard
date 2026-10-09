"""Tests for packaging/scripts/collect-licenses.py.

Run from the repo root: uv run --no-project --with pytest pytest packaging/tests
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collect-licenses.py"


def load_collector() -> ModuleType:
    spec = importlib.util.spec_from_file_location("collect_licenses", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: the dataclass in the script looks its module up in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collect = load_collector()


def make_dist(
    site: Path,
    name: str,
    version: str,
    *,
    expression: str | None = None,
    files: dict[str, str] | None = None,
) -> Path:
    dist = site / f"{name}-{version}.dist-info"
    dist.mkdir(parents=True)
    lines = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
    if expression:
        lines.append(f"License-Expression: {expression}")
    (dist / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for rel, text in (files or {}).items():
        path = dist / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return dist


def section_titles(text: str) -> list[str]:
    """A header is a non-empty line whose next line is an '=' underline of the same length."""
    lines = text.splitlines()
    return [
        lines[i - 1]
        for i in range(1, len(lines))
        if lines[i] and set(lines[i]) == {"="} and len(lines[i]) == len(lines[i - 1])
    ]


@pytest.fixture
def app_license(tmp_path: Path) -> Path:
    path = tmp_path / "LICENSE"
    path.write_text("Apache License text for the app\n", encoding="utf-8")
    return path


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    root = tmp_path / "python"
    lib = root / "lib" / "python3.13"
    site = lib / "site-packages"
    site.mkdir(parents=True)
    (lib / "LICENSE.txt").write_text(
        "Python Software Foundation License text\n", encoding="utf-8"
    )
    make_dist(
        site,
        "alpha",
        "1.0",
        expression="MIT",
        files={"licenses/LICENSE": "alpha license\n"},
    )
    make_dist(
        site,
        "beta",
        "2.0",
        files={"LICENSE-APACHE": "beta apache\n", "LICENSE-MIT": "beta mit\n"},
    )
    # The app's own wheel carries no license file; its section points at the app section.
    make_dist(site, "splash_gui", "0.1.0")
    return root


def test_writes_app_then_cpython_then_one_section_per_distribution(
    tmp_path: Path, runtime: Path, app_license: Path
) -> None:
    out = tmp_path / "licenses" / "THIRD_PARTY.txt"
    assert (
        collect.main(
            [
                "--runtime",
                str(runtime),
                "--app-license",
                str(app_license),
                "--out",
                str(out),
            ]
        )
        == 0
    )

    text = out.read_text(encoding="utf-8")
    assert text.startswith(
        "Splashboard (Apache-2.0)\n" + "=" * len("Splashboard (Apache-2.0)") + "\n"
    )
    titles = section_titles(text)
    assert titles == [
        "Splashboard (Apache-2.0)",
        "CPython 3.13 (PSF-2.0)",
        "alpha 1.0 (MIT)",
        "beta 2.0",
        "splash_gui 0.1.0",
    ]
    # Installed distributions plus two: the app section and CPython.
    assert len(titles) == 3 + 2
    assert "alpha license" in text
    assert "--- LICENSE-APACHE ---" in text and "beta mit" in text
    assert "Part of Splashboard" in text
    assert not list(out.parent.glob("*.partial"))


def test_fails_when_one_package_has_no_license_file(
    tmp_path: Path, runtime: Path, app_license: Path, capsys
) -> None:
    site = runtime / "lib" / "python3.13" / "site-packages"
    make_dist(site, "gamma", "3.0", expression="MIT")  # METADATA only, no license file
    out = tmp_path / "THIRD_PARTY.txt"

    rc = collect.main(
        [
            "--runtime",
            str(runtime),
            "--app-license",
            str(app_license),
            "--out",
            str(out),
        ]
    )

    assert rc == 1
    assert not out.exists()
    err = capsys.readouterr().err
    assert "no license file in gamma-3.0.dist-info" in err
    assert "gamma 3.0" in err


def test_an_empty_license_file_does_not_count(
    tmp_path: Path, runtime: Path, app_license: Path, capsys
) -> None:
    site = runtime / "lib" / "python3.13" / "site-packages"
    make_dist(site, "delta", "4.0", files={"LICENSE": "   \n"})
    out = tmp_path / "THIRD_PARTY.txt"

    assert (
        collect.main(
            [
                "--runtime",
                str(runtime),
                "--app-license",
                str(app_license),
                "--out",
                str(out),
            ]
        )
        == 1
    )
    assert "delta 4.0" in capsys.readouterr().err
    assert not out.exists()


def test_fails_without_a_cpython_license(
    tmp_path: Path, runtime: Path, app_license: Path, capsys
) -> None:
    (runtime / "lib" / "python3.13" / "LICENSE.txt").unlink()
    out = tmp_path / "THIRD_PARTY.txt"

    assert (
        collect.main(
            [
                "--runtime",
                str(runtime),
                "--app-license",
                str(app_license),
                "--out",
                str(out),
            ]
        )
        == 1
    )
    assert "no CPython license" in capsys.readouterr().err
    assert not out.exists()


def test_fails_without_the_app_license(tmp_path: Path, runtime: Path, capsys) -> None:
    out = tmp_path / "THIRD_PARTY.txt"

    rc = collect.main(
        [
            "--runtime",
            str(runtime),
            "--app-license",
            str(tmp_path / "missing"),
            "--out",
            str(out),
        ]
    )

    assert rc == 1
    assert "no app license" in capsys.readouterr().err


def test_a_top_level_notice_counts(
    tmp_path: Path, runtime: Path, app_license: Path
) -> None:
    site = runtime / "lib" / "python3.13" / "site-packages"
    make_dist(site, "epsilon", "5.0", files={"NOTICE.md": "epsilon notice\n"})
    out = tmp_path / "THIRD_PARTY.txt"

    assert (
        collect.main(
            [
                "--runtime",
                str(runtime),
                "--app-license",
                str(app_license),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert "epsilon 5.0" in section_titles(out.read_text(encoding="utf-8"))


def test_a_license_only_below_the_dist_info_top_level_does_not_count(
    tmp_path: Path, runtime: Path, app_license: Path, capsys
) -> None:
    site = runtime / "lib" / "python3.13" / "site-packages"
    make_dist(site, "zeta", "6.0", files={"docs/LICENSE": "nested only\n"})
    out = tmp_path / "THIRD_PARTY.txt"

    assert (
        collect.main(
            [
                "--runtime",
                str(runtime),
                "--app-license",
                str(app_license),
                "--out",
                str(out),
            ]
        )
        == 1
    )
    assert "zeta 6.0" in capsys.readouterr().err
    assert not out.exists()

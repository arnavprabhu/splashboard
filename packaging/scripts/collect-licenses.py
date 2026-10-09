#!/usr/bin/env python3
"""Writes THIRD_PARTY.txt for the packaged Splashboard.app (docs/plans/packaging.md, PKG-5).

    python3 -I -B packaging/scripts/collect-licenses.py \\
        --runtime Contents/Resources/manager/python \\
        --app-license LICENSE \\
        --out Contents/Resources/licenses/THIRD_PARTY.txt

--runtime is the bundled interpreter's python/ folder. It must hold lib/python3.X/LICENSE.txt (CPython's
license) and lib/python3.X/site-packages/*.dist-info. The file starts with a section for the app, then one
section for CPython, then one section per installed distribution, sorted by name. A section is a header
line underlined with '='. A distribution's section holds the license files it ships: every file under its
dist-info licenses/ folder, or a LICENSE, COPYING or NOTICE file at the top of the dist-info. The app's own
distribution (splash_gui) has no license file of its own, so its section points at the app section.

Exit status: 0 when the file is written. 1 when a distribution has no license text, or when the runtime has
no CPython license or site-packages. On failure nothing is written and the reasons go to stderr.

Standard library only, so the bundled interpreter runs it (python -I -B) and so does any Python 3.12+.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

APP_HEADER = "Splashboard (Apache-2.0)"
# Normalized name of the app's own wheel. Its license is the app section, not a file in the wheel.
APP_DISTRIBUTION = "splash-gui"
LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice)([._-].*)?$", re.IGNORECASE)


class LicenseError(Exception):
    """A problem that stops the file from being written."""


@dataclass(frozen=True)
class Section:
    title: str
    body: str


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip("\n") + "\n"


def find_python_lib(runtime: Path) -> Path:
    libs = sorted(p for p in (runtime / "lib").glob("python3.*") if p.is_dir())
    if len(libs) != 1:
        raise LicenseError(
            f"expected one lib/python3.X folder under {runtime}, found {len(libs)}"
        )
    return libs[0]


def license_files(dist_dir: Path) -> list[Path]:
    """Files of one dist-info that carry a license: its licenses/ folder, or a top-level LICENSE-like file."""
    found: list[Path] = []
    for path in sorted(dist_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(dist_dir)
        carries_license = rel.parts[0] == "licenses" or (
            len(rel.parts) == 1 and LICENSE_FILE.match(path.name)
        )
        if carries_license and path.read_bytes().strip():
            found.append(path)
    return found


def distribution_section(dist_dir: Path) -> Section:
    meta = importlib.metadata.PathDistribution(dist_dir).metadata
    if meta is None:
        raise LicenseError(f"{dist_dir.name} has no METADATA file")
    name = meta.get("Name") or dist_dir.name
    version = meta.get("Version") or ""
    expression = (meta.get("License-Expression") or "").strip()
    title = " ".join(part for part in (name, version) if part)
    if expression:
        title += f" ({expression})"
    if normalize(name) == APP_DISTRIBUTION:
        return Section(
            title, "Part of Splashboard. Its license is the one in the first section.\n"
        )
    files = license_files(dist_dir)
    if not files:
        raise LicenseError(f"no license file in {dist_dir.name} ({title})")
    if len(files) == 1:
        return Section(title, read_text(files[0]))
    parts = [
        f"--- {f.relative_to(dist_dir).as_posix()} ---\n{read_text(f)}" for f in files
    ]
    return Section(title, "\n".join(parts))


def collect(runtime: Path, app_license: Path) -> list[Section]:
    if not app_license.is_file():
        raise LicenseError(f"no app license at {app_license}")
    lib = find_python_lib(runtime)
    cpython = lib / "LICENSE.txt"
    if not cpython.is_file():
        raise LicenseError(f"no CPython license at {cpython}")
    site = lib / "site-packages"
    if not site.is_dir():
        raise LicenseError(f"no site-packages folder at {site}")

    cpython_version = lib.name.removeprefix("python")
    sections = [
        Section(APP_HEADER, read_text(app_license)),
        Section(f"CPython {cpython_version} (PSF-2.0)", read_text(cpython)),
    ]
    dist_dirs = sorted(site.glob("*.dist-info"), key=lambda p: normalize(p.name))
    missing: list[str] = []
    for dist_dir in dist_dirs:
        try:
            sections.append(distribution_section(dist_dir))
        except LicenseError as err:
            missing.append(str(err))
    if missing:
        raise LicenseError("; ".join(missing))
    return sections


def render(sections: list[Section]) -> str:
    parts = [f"{s.title}\n{'=' * len(s.title)}\n\n{s.body}" for s in sections]
    return "\n\n".join(parts)


def write_atomic(out: Path, text: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write THIRD_PARTY.txt for the packaged app."
    )
    parser.add_argument(
        "--runtime", required=True, type=Path, help="the bundled python/ folder"
    )
    parser.add_argument(
        "--app-license", required=True, type=Path, help="the app's LICENSE file"
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="where to write THIRD_PARTY.txt"
    )
    args = parser.parse_args(argv)
    try:
        sections = collect(args.runtime, args.app_license)
        write_atomic(args.out, render(sections))
    except (LicenseError, OSError) as err:
        print(f"collect-licenses: {err}", file=sys.stderr)
        return 1
    print(f"collect-licenses: {len(sections)} sections -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

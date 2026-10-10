"""Find the Splash engine and its package.

Order: the `engine.path` setting → `SPLASH_GUI_REAL_SPLASH` →
`$(brew --prefix)/opt/splash/bin/splash` → the first `splash` on PATH that is
not our own shim. Then `splash --version` gives the version and the package
directory (PKG) gives the bundled Python, `install/` and `server/`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

REAL_SPLASH_ENV = "SPLASH_GUI_REAL_SPLASH"
# Our CLI shim (~/.splash/bin/splash) must contain this marker so discovery skips it.
SHIM_MARKER = "SPLASH_GUI_SHIM"
# The `splash` script pip and uv install for this package (`[project.scripts]`). It is our
# CLI too, so it is never the engine: taking it would make the CLI exec itself forever.
ENTRY_POINT_MARKER = "from splash_gui.cli import"
# 1.3.1 is the first that loads every MLX quantization the GUI offers and refuses
# Splash packages, which the GUI no longer lists.
SUPPORTED_MIN = (1, 3, 1)
SUPPORTED_BELOW = (1, 4, 0)
SUPPORTED_RANGE = ">=1.3.1 <1.4.0"
BREW_TIMEOUT_S = 10
VERSION_TIMEOUT_S = 15
_VERSION_RE = re.compile(r"Splash\s+(\d+)\.(\d+)\.(\d+)(\S*)")
_LAUNCHER_RE = re.compile(r"""["']?([^"'\s]+)/install/launcher\.py""")

Source = Literal["setting", "env", "brew", "path"]
Support = Literal["supported", "untested", "too_old", "unknown"]

Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


def run_command(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout, check=False, env=env
    )


@dataclass(frozen=True)
class EngineInfo:
    found: bool
    cli: Path | None = None
    source: Source | None = None
    version: str | None = None
    version_tuple: tuple[int, int, int] | None = None
    support: Support = "unknown"
    source_checkout: bool = False
    pkg: Path | None = None
    python: Path | None = None
    install_dir: Path | None = None
    server_dir: Path | None = None
    brew_prefix: Path | None = None
    error: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def banner(self) -> str | None:
        if self.support == "untested" and self.version_tuple:
            major, minor, _ = self.version_tuple
            return f"GUI untested with Splash {major}.{minor}"
        if self.support == "too_old" and self.version:
            return f"Splash {self.version} is older than 1.3.1; upgrade the engine"
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "found": self.found,
            "cli": str(self.cli) if self.cli else None,
            "source": self.source,
            "version": self.version,
            "support": self.support,
            "supported_range": SUPPORTED_RANGE,
            "banner": self.banner,
            "source_checkout": self.source_checkout,
            "pkg": str(self.pkg) if self.pkg else None,
            "python": str(self.python) if self.python else None,
            "error": self.error,
        }


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def support_for(version: tuple[int, int, int] | None) -> Support:
    if version is None:
        return "unknown"
    if version < SUPPORTED_MIN:
        return "too_old"
    if version >= SUPPORTED_BELOW:
        return "untested"
    return "supported"


def is_shim(path: Path, shim_paths: Sequence[Path] = ()) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if any(resolved == s.resolve() for s in shim_paths if s.exists()):
        return True
    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return SHIM_MARKER.encode() in head or ENTRY_POINT_MARKER.encode() in head


def which_all(name: str, path_env: str | None) -> list[Path]:
    found: list[Path] = []
    for directory in (path_env or "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK) and candidate not in found:
            found.append(candidate)
    return found


def brew_prefix(runner: Runner = run_command, brew: str | None = None) -> Path | None:
    brew = (
        brew
        or shutil.which("brew")
        or next(
            (b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if Path(b).exists()), None
        )
    )
    if brew is None:
        return None
    try:
        result = runner([brew, "--prefix"], BREW_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = result.stdout.strip()
    return Path(text) if result.returncode == 0 and text else None


def package_for(cli: Path) -> tuple[Path | None, bool]:
    """PKG for an engine CLI, and whether it is a source checkout.

    Homebrew's `bin/splash` is a sh wrapper that execs `<PKG>/install/launcher.py`
    with `<PKG>/python/bin/python3`; a source checkout's `./splash` sits in PKG.
    """
    try:
        resolved = cli.resolve()
    except OSError:
        return None, False
    try:
        head = resolved.read_bytes()[:8192].decode("utf-8", "replace")
    except OSError:
        head = ""
    match = _LAUNCHER_RE.search(head)
    if match:
        pkg = Path(match.group(1))
        if (pkg / "server").is_dir():
            return pkg, not (pkg / "release.json").is_file()
    for candidate in (resolved.parent.parent / "libexec", resolved.parent):
        if (candidate / "install" / "launcher.py").is_file():
            return candidate, not (candidate / "release.json").is_file()
    return None, False


def _python_for(pkg: Path, source_checkout: bool) -> Path | None:
    # install/paths.py: PYTHON = ROOT / ("python/bin/python3" if PACKAGED else ".venv/bin/python")
    python = pkg / (".venv/bin/python" if source_checkout else "python/bin/python3")
    return python if python.exists() else None


def _release_version(pkg: Path | None) -> str | None:
    if pkg is None:
        return None
    try:
        value = json.loads((pkg / "release.json").read_text())["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return str(value)


def candidates(
    setting: str | None,
    env: Mapping[str, str],
    prefix: Path | None,
    shim_paths: Sequence[Path],
) -> list[tuple[Source, Path]]:
    out: list[tuple[Source, Path]] = []
    if setting:
        out.append(("setting", Path(setting).expanduser()))
    if env.get(REAL_SPLASH_ENV):
        out.append(("env", Path(env[REAL_SPLASH_ENV]).expanduser()))
    if prefix is not None:
        out.append(("brew", prefix / "opt" / "splash" / "bin" / "splash"))
    for path in which_all("splash", env.get("PATH")):
        if not is_shim(path, shim_paths):
            out.append(("path", path))
    return out


def discover(
    setting: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    shim_paths: Sequence[Path] = (),
    runner: Runner = run_command,
    prefix: Path | Literal["auto"] | None = "auto",
) -> EngineInfo:
    """Locate the engine. Never raises; `found=False` carries the reason."""
    env = os.environ if env is None else env
    resolved_prefix = brew_prefix(runner) if prefix == "auto" else prefix
    notes: list[str] = []
    for source, cli in candidates(setting, env, resolved_prefix, shim_paths):
        if not (cli.is_file() and os.access(cli, os.X_OK)):
            if source in ("setting", "env"):
                notes.append(f"{source} engine path {cli} is not an executable file")
            continue
        if is_shim(cli, shim_paths):
            notes.append(f"skipped Splashboard's own shim at {cli}")
            continue
        return _inspect(cli, source, resolved_prefix, runner, tuple(notes))
    return EngineInfo(
        found=False,
        brew_prefix=resolved_prefix,
        notes=tuple(notes),
        error="Splash is not installed. Install it with: brew install incoai/tap/splash",
    )


def _inspect(
    cli: Path, source: Source, prefix: Path | None, runner: Runner, notes: tuple[str, ...]
) -> EngineInfo:
    pkg, source_checkout = package_for(cli)
    error: str | None = None
    version_text = ""
    try:
        result = runner([str(cli), "--version"], VERSION_TIMEOUT_S)
        version_text = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            error = f"splash --version failed: {version_text or result.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        error = f"splash --version failed: {exc}"
    version_tuple = parse_version(version_text)
    if version_tuple is None and "source checkout" in version_text:
        source_checkout = True
    if version_tuple is None:
        release = _release_version(pkg)
        if release:
            version_tuple = parse_version(f"Splash {release}")
    version = ".".join(map(str, version_tuple)) if version_tuple else None
    python = _python_for(pkg, source_checkout) if pkg else None
    if pkg is None:
        error = error or f"cannot find the Splash package directory for {cli}"
    elif python is None:
        error = error or f"Splash's Python is missing under {pkg}"
    return EngineInfo(
        found=True,
        cli=cli,
        source=source,
        version=version,
        version_tuple=version_tuple,
        support=support_for(version_tuple),
        source_checkout=source_checkout,
        pkg=pkg,
        python=python,
        install_dir=pkg / "install" if pkg else None,
        server_dir=pkg / "server" if pkg else None,
        brew_prefix=prefix,
        error=error,
        notes=notes,
    )

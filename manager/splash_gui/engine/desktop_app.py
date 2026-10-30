"""The Splash desktop app (Splash 1.5.0, bundle `ai.inco.splash`), which may sit beside
the Homebrew engine Splashboard runs.

The app bundles its own runtime but shares Splash's data folder
(`~/Library/Application Support/Splash`: the installed models' selection links and
assemblies) with the command line. Its release notes ask to keep the two on the same
release: switching between releases rebuilds a GGUF's metadata from its weights and
starts the persistent caches of GGUF and MLX MoE models fresh, every time.
"""

from __future__ import annotations

import json
import plistlib
from dataclasses import dataclass
from pathlib import Path

BUNDLE_ID = "ai.inco.splash"
# Where a drag-install or `brew install --cask incoai/tap/splash-app` puts it.
CANDIDATES: tuple[Path, ...] = (
    Path("/Applications/Splash.app"),
    Path.home() / "Applications" / "Splash.app",
)


@dataclass(frozen=True)
class SplashApp:
    path: Path
    version: str | None


def find_splash_app(candidates: tuple[Path, ...] | None = None) -> SplashApp | None:
    """The installed Splash desktop app and the Splash release it bundles, or None. The
    version is the bundled runtime's `release.json`, else the app's short version."""
    for path in CANDIDATES if candidates is None else candidates:
        try:
            info = plistlib.loads((path / "Contents" / "Info.plist").read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        if not isinstance(info, dict) or info.get("CFBundleIdentifier") != BUNDLE_ID:
            continue
        version = None
        try:
            release = json.loads(
                (path / "Contents" / "Resources" / "splash" / "release.json").read_text()
            )
            if isinstance(release, dict) and isinstance(release.get("version"), str):
                version = release["version"]
        except (OSError, ValueError):
            pass
        if version is None and isinstance(info.get("CFBundleShortVersionString"), str):
            version = info["CFBundleShortVersionString"]
        return SplashApp(path, version)
    return None

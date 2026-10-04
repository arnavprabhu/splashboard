"""Fake counterpart of splash/install/paths.py.

The real packaged install hardcodes DATA to ~/Library/Application Support/Splash.
The fake must never touch that directory, so DATA comes from
SPLASH_GUI_FAKE_DATA, else a directory under the system temp dir.
"""

import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGED = (ROOT / "release.json").is_file()


def _data() -> Path:
    configured = os.environ.get("SPLASH_GUI_FAKE_DATA")
    if configured:
        return Path(configured).expanduser().absolute()
    return Path(tempfile.gettempdir()) / "splash-gui-fake-data"


DATA = _data()
MODELS = DATA / "models"
RUNTIME = DATA / "runtime"
PYTHON = ROOT / "python/bin/python3"
BINARY = ROOT / "engine/splash"


def hub_cache() -> Path:
    """Where downloads go: HF_HUB_CACHE as huggingface_hub reads it, else a
    folder of the fake data dir (never the user's ~/.cache/huggingface)."""
    configured = os.environ.get("HF_HUB_CACHE")
    if configured:
        return Path(configured).expanduser().absolute()
    return DATA / "hub"


def default_cache_dir() -> Path:
    """--persistent-cache without --cache-dir. The real default is
    ~/Library/Caches/Splash/prefix-cache (serve_options.DEFAULT_CACHE_DIR);
    the fake keeps it inside its data dir."""
    return DATA / "prefix-cache"

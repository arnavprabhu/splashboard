"""The ~/.splash layout (SPEC §5).

Everything hangs off one base directory, `SPLASH_GUI_HOME` (default `~/.splash`),
so tests and alternative installs never touch the real one.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

HOME_ENV = "SPLASH_GUI_HOME"
DIR_MODE = 0o700
FILE_MODE = 0o600

# Splash hardcodes its per-user data directory in a packaged install
# (splash/install/paths.py: DATA = ~/Library/Application Support/Splash).
SPLASH_DATA_DIR = Path.home() / "Library" / "Application Support" / "Splash"
# Splash writes crash traces here and nowhere else (server/crash_trace.py,
# DEFAULT_TRACE_DIRECTORY); there is no environment override.
SPLASH_CRASH_TRACE_DIR = Path.home() / "Library" / "Logs" / "Splash" / "crash"
# The fake engine (scripts/fake_splash) reads its stand-in for SPLASH_DATA_DIR from
# this variable; when it is set the manager reads the same directory, so tests never
# touch ~/Library/Application Support/Splash. The real engine ignores it.
FAKE_DATA_ENV = "SPLASH_GUI_FAKE_DATA"


def splash_data_dir() -> Path:
    """Splash's per-user data directory (selection links, assemblies, runtime locks)."""
    override = os.environ.get(FAKE_DATA_ENV)
    return Path(override).expanduser().absolute() if override else SPLASH_DATA_DIR


def splash_models_dir() -> Path:
    """Where Splash keeps its selection links (`install/paths.py` MODELS)."""
    return splash_data_dir() / "models"


def default_base() -> Path:
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser().absolute()
    return Path.home() / ".splash"


@dataclass(frozen=True)
class Paths:
    """Every manager-owned path. Construct with `Paths.from_env()` or an explicit base."""

    base: Path

    @classmethod
    def from_env(cls) -> Paths:
        return cls(default_base())

    @property
    def settings_file(self) -> Path:
        return self.base / "settings.json"

    @property
    def models_dir(self) -> Path:
        """Default HF_HUB_CACHE; `storage.models_dir` may move it."""
        return self.base / "models"

    @property
    def cache_dir(self) -> Path:
        """Default --cache-dir; `storage.cache_dir` may move it."""
        return self.base / "cache"

    @property
    def chats_dir(self) -> Path:
        return self.base / "chats"

    @property
    def attachments_dir(self) -> Path:
        return self.chats_dir / "attachments"

    @property
    def usage_db(self) -> Path:
        return self.base / "usage.db"

    @property
    def downloads_file(self) -> Path:
        return self.base / "downloads.json"

    @property
    def integrations_dir(self) -> Path:
        return self.base / "integrations"

    @property
    def integrations_state(self) -> Path:
        return self.integrations_dir / "state.json"

    @property
    def integrations_backups(self) -> Path:
        return self.integrations_dir / "backups"

    @property
    def codex_app_dir(self) -> Path:
        return self.integrations_dir / "codex-app"

    @property
    def logs_dir(self) -> Path:
        return self.base / "logs"

    @property
    def manager_log(self) -> Path:
        return self.logs_dir / "manager.log"

    @property
    def engine_log(self) -> Path:
        return self.logs_dir / "engine.log"

    @property
    def traces_dir(self) -> Path:
        return self.logs_dir / "traces"

    @property
    def run_dir(self) -> Path:
        return self.base / "run"

    @property
    def manager_pid(self) -> Path:
        return self.run_dir / "manager.pid"

    @property
    def engine_pid(self) -> Path:
        return self.run_dir / "engine.pid"

    @property
    def cli_token(self) -> Path:
        return self.run_dir / "cli.token"

    @property
    def bin_dir(self) -> Path:
        return self.base / "bin"

    @property
    def shim(self) -> Path:
        return self.bin_dir / "splash"

    def directories(self) -> tuple[Path, ...]:
        return (
            self.base,
            self.models_dir,
            self.cache_dir,
            tmp_dir_for(self.cache_dir),
            self.chats_dir,
            self.attachments_dir,
            self.integrations_dir,
            self.integrations_backups,
            self.codex_app_dir,
            self.logs_dir,
            self.traces_dir,
            self.run_dir,
            self.bin_dir,
        )

    def ensure(self) -> Paths:
        """Create every directory with mode 0700 (existing ones are tightened)."""
        for directory in self.directories():
            ensure_private_dir(directory)
        return self


def tmp_dir_for(cache_dir: Path) -> Path:
    """TMPDIR for the engine: Splash's session-only SSD slot files go here (SPEC §5)."""
    return cache_dir / "tmp"


def ensure_private_dir(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    if directory.stat().st_mode & 0o777 != DIR_MODE:
        directory.chmod(DIR_MODE)
    return directory


def expand(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().absolute()


def write_atomic(path: Path, data: bytes, mode: int = FILE_MODE) -> None:
    """Write `data` to `path` atomically: temp file in the same directory, fsync, rename."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

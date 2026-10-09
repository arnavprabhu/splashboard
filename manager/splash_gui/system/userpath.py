"""Where the user's own command-line programs are.

The packaged manager runs as a LaunchAgent, and launchd gives it only the system folders
and Homebrew on PATH. Coding agents usually live elsewhere (`~/.local/bin`, `~/.opencode/bin`,
npm or bun folders), which an interactive shell adds through its rc files. So programs are
looked up on the login shell's PATH, read once and cached, plus the common user folders.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

SHELL_TIMEOUT_S = 3.0
CACHE_S = 300.0
# Folders installers commonly use, searched after the shell's PATH.
USER_DIRS = (
    ".local/bin",
    ".opencode/bin",
    ".bun/bin",
    ".npm-global/bin",
    ".cargo/bin",
    ".deno/bin",
    "bin",
)
SYSTEM_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

_lock = threading.Lock()
_shell_cache: tuple[float, str] | None = None


def _shell_path() -> str:
    """PATH as the user's interactive login shell sets it, or "" when it cannot be read."""
    shell = os.environ.get("SHELL") or "/bin/zsh"
    if not Path(shell).is_file():
        return ""
    try:
        result = subprocess.run(
            [shell, "-l", "-i", "-c", 'printf "__PATH__%s" "$PATH"'],
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    # rc files may print banners first; the marker finds our line.
    _, _, path = result.stdout.rpartition("__PATH__")
    return path.strip()


def shell_path() -> str:
    """`_shell_path`, cached for a few minutes: reading it starts a shell."""
    global _shell_cache
    with _lock:
        now = time.monotonic()
        if _shell_cache is None or now - _shell_cache[0] >= CACHE_S:
            _shell_cache = (now, _shell_path())
        return _shell_cache[1]


def user_path(home: Path | None = None) -> str:
    """The manager's PATH, then the login shell's, then the common user folders under
    `home`, without duplicates."""
    base = home or Path.home()
    parts: list[str] = []
    for chunk in (os.environ.get("PATH", ""), shell_path()):
        parts.extend(p for p in chunk.split(os.pathsep) if p)
    parts.extend(str(base / d) for d in USER_DIRS)
    parts.extend(SYSTEM_DIRS)
    seen: set[str] = set()
    unique = [p for p in parts if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]
    return os.pathsep.join(unique)


def which(name: str, home: Path | None = None) -> str | None:
    """`shutil.which` on the user's PATH (see `user_path`)."""
    return shutil.which(name, path=user_path(home))

"""Read Splash's serve options at runtime through a subprocess helper.

The helper runs with Splash's bundled Python and `PYTHONPATH=PKG`; Splash modules
are never imported into the manager, so a broken engine install cannot crash it.
"""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .discovery import EngineInfo

HELPER_TIMEOUT_S = 20
HELPER = Path(__file__).resolve().parent.parent / "helpers" / "serve_options_dump.py"

# Every option that maps to a setting (or that the GUI always manages).
KNOWN_FLAGS = frozenset(
    {
        "--model",
        "--revision",
        "--draft-model",
        "--language-only",
        "--offline",
        "--host",
        "--port",
        "--served-model-name",
        "--announce-served-name",
        "--default-reasoning-effort",
        "--kv-format",
        "--max-memory",
        "--max-cache-disk",
        "--persistent-cache",
        "--cache-dir",
        "--max-context",
        "--decode-share",
        "--allowed-host",
        "--allowed-origin",
        "--max-request-size",
        "--max-image-pixels",
        "--request-timeout",
        "--queue-size",
        "--idle-release",
        "--disable-ane",
        "--allow-idle-sleep",
        "--api-key",
        "--no-webui",
    }
)


@dataclass(frozen=True)
class EngineOption:
    flag: str
    dest: str
    default: Any
    help: str
    choices: list[Any] | None
    metavar: str | None
    action: str
    environment: str | None
    secret: bool
    source: str

    @property
    def takes_value(self) -> bool:
        return self.action not in ("store_true", "store_false")

    @property
    def known(self) -> bool:
        return self.flag in KNOWN_FLAGS

    def as_dict(self) -> dict[str, Any]:
        return {
            "flag": self.flag,
            "dest": self.dest,
            "default": self.default,
            "help": self.help,
            "choices": self.choices,
            "metavar": self.metavar,
            "action": self.action,
            "environment": self.environment,
            "secret": self.secret,
            "source": self.source,
            "takes_value": self.takes_value,
            "known": self.known,
        }


@dataclass(frozen=True)
class EngineOptions:
    available: bool
    version: str | None = None
    options: tuple[EngineOption, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)

    def by_flag(self) -> dict[str, EngineOption]:
        return {o.flag: o for o in self.options}

    def unknown(self) -> list[EngineOption]:
        """Options with no setting: shown as the raw extra_flags editor."""
        return [o for o in self.options if not o.known]


def helper_env(pkg: Path) -> dict[str, str]:
    import os

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTHON", "SPLASH_")) and k not in ("VIRTUAL_ENV",)
    }
    env["PYTHONPATH"] = str(pkg)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def parse_helper_output(text: str) -> EngineOptions:
    data = json.loads(text)
    options = tuple(
        EngineOption(
            flag=str(o["flag"]),
            dest=str(o.get("dest", "")),
            default=o.get("default"),
            help=str(o.get("help") or ""),
            choices=o.get("choices"),
            metavar=o.get("metavar"),
            action=str(o.get("action") or "store"),
            environment=o.get("environment"),
            secret=bool(o.get("secret")),
            source=str(o.get("source") or "shared"),
        )
        for o in data.get("options", [])
        if isinstance(o, dict) and isinstance(o.get("flag"), str)
    )
    return EngineOptions(
        available=bool(options),
        version=data.get("version"),
        options=options,
        errors=tuple(str(e) for e in data.get("errors", [])),
    )


def run_helper(engine: EngineInfo, timeout: float = HELPER_TIMEOUT_S) -> EngineOptions:
    if not engine.found or engine.python is None or engine.pkg is None:
        return EngineOptions(available=False, errors=(engine.error or "engine not found",))
    try:
        result = subprocess.run(
            [str(engine.python), "-P", str(HELPER)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=helper_env(engine.pkg),
            cwd=str(engine.pkg),
        )
    except subprocess.TimeoutExpired:
        return EngineOptions(available=False, errors=(f"helper timed out after {timeout:g} s",))
    except OSError as error:
        return EngineOptions(available=False, errors=(f"cannot run Splash's Python: {error}",))
    if result.returncode != 0:
        tail = (result.stderr.strip().splitlines() or [str(result.returncode)])[-1]
        return EngineOptions(available=False, errors=(f"helper failed: {tail}",))
    try:
        return parse_helper_output(result.stdout)
    except (ValueError, TypeError, KeyError) as error:
        return EngineOptions(available=False, errors=(f"helper output unreadable: {error}",))


class EngineOptionsCache:
    """Runs the helper once per engine install (CLI path + version)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # One helper run at a time: the startup read and a first Settings
        # load share it, so the second caller takes the first one's result.
        self._run_lock = threading.Lock()
        self._key: tuple[str | None, str | None] | None = None
        self._value: EngineOptions | None = None

    def get(self, engine: EngineInfo) -> EngineOptions:
        key = (str(engine.cli) if engine.cli else None, engine.version)
        with self._lock:
            if self._key == key and self._value is not None:
                return self._value
        with self._run_lock:
            with self._lock:
                if self._key == key and self._value is not None:
                    return self._value
            value = run_helper(engine)
            with self._lock:
                self._key, self._value = key, value
            return value

    def clear(self) -> None:
        with self._lock:
            self._key, self._value = None, None

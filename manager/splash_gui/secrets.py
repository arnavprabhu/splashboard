"""Secrets (SPEC §17.2): the API key, HF token override and admin session secret.

Production uses the macOS Keychain through the `security` CLI (generic passwords).
`SPLASH_GUI_SECRETS=memory|file|keychain` selects a backend; tests use `memory`.
Secrets never go into settings.json and are redacted from logs and exports.
"""

from __future__ import annotations

import json
import os
import re
import secrets as _stdlib_secrets
import subprocess
import sys
import threading
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .paths import Paths, write_atomic

BACKEND_ENV = "SPLASH_GUI_SECRETS"
ACCOUNT = "splash-gui"
SECURITY_TIMEOUT_S = 10
REDACTED = "••••••"


class SecretName(StrEnum):
    API_KEY = "ai.splashgui.apikey"
    HF_TOKEN = "ai.splashgui.hf"  # noqa: S105 (a Keychain service name)
    SESSION = "ai.splashgui.session"


class SecretsError(RuntimeError):
    pass


class SecretsBackend(Protocol):
    name: str

    def get(self, name: SecretName) -> str | None: ...

    def set(self, name: SecretName, value: str) -> None: ...

    def delete(self, name: SecretName) -> None: ...


def _check_value(value: str) -> str:
    # Visible ASCII only, as Splash's validate_api_key requires for keys; quotes and
    # backslashes are refused because `security -i` parses its input line.
    if not value or any(ord(c) <= 32 or ord(c) >= 127 or c in '"\\' for c in value):
        raise SecretsError("secret must be visible ASCII without spaces, quotes or backslashes")
    return value


class MemoryBackend:
    name = "memory"

    def __init__(self) -> None:
        self._values: dict[SecretName, str] = {}
        self._lock = threading.Lock()

    def get(self, name: SecretName) -> str | None:
        with self._lock:
            return self._values.get(name)

    def set(self, name: SecretName, value: str) -> None:
        with self._lock:
            self._values[name] = _check_value(value)

    def delete(self, name: SecretName) -> None:
        with self._lock:
            self._values.pop(name, None)


class FileBackend:
    """A 0600 JSON file. For tests and non-macOS development only."""

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise SecretsError(f"cannot read {self.path}: {error}") from error
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    def get(self, name: SecretName) -> str | None:
        with self._lock:
            return self._read().get(name.value)

    def set(self, name: SecretName, value: str) -> None:
        with self._lock:
            data = self._read()
            data[name.value] = _check_value(value)
            write_atomic(self.path, json.dumps(data, indent=2).encode())

    def delete(self, name: SecretName) -> None:
        with self._lock:
            data = self._read()
            if data.pop(name.value, None) is not None:
                write_atomic(self.path, json.dumps(data, indent=2).encode())


class KeychainBackend:
    """Generic passwords in the login keychain, via /usr/bin/security.

    Values are written through `security -i` on stdin, never on the command line,
    where other processes could read them.
    """

    name = "keychain"
    NOT_FOUND = 44  # errSecItemNotFound as security's exit status

    def __init__(self, security: str = "/usr/bin/security", account: str = ACCOUNT) -> None:
        self.security = security
        self.account = account

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.security, *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=SECURITY_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SecretsError(f"keychain unavailable: {error}") from error

    def get(self, name: SecretName) -> str | None:
        result = self._run(["find-generic-password", "-s", name.value, "-a", self.account, "-w"])
        if result.returncode == self.NOT_FOUND:
            return None
        if result.returncode != 0:
            raise SecretsError(f"keychain read failed: {result.stderr.strip()}")
        return result.stdout.rstrip("\n") or None

    def set(self, name: SecretName, value: str) -> None:
        value = _check_value(value)
        command = (
            f'add-generic-password -U -s "{name.value}" -a "{self.account}" '
            f'-l "Splash GUI" -w "{value}"\n'
        )
        result = self._run(["-i"], stdin=command)
        if result.returncode != 0:
            raise SecretsError(f"keychain write failed: {result.stderr.strip()}")

    def delete(self, name: SecretName) -> None:
        result = self._run(["delete-generic-password", "-s", name.value, "-a", self.account])
        if result.returncode not in (0, self.NOT_FOUND):
            raise SecretsError(f"keychain delete failed: {result.stderr.strip()}")


def backend_from_env(paths: Paths, env: Mapping[str, str] | None = None) -> SecretsBackend:
    env = os.environ if env is None else env
    choice = env.get(BACKEND_ENV, "").strip().lower()
    if not choice:
        choice = "keychain" if sys.platform == "darwin" else "file"
    if choice == "memory":
        return MemoryBackend()
    if choice == "file":
        return FileBackend(paths.run_dir / "secrets.json")
    if choice == "keychain":
        return KeychainBackend()
    raise SecretsError(f"{BACKEND_ENV} must be keychain, file or memory, not {choice!r}")


class SecretStore:
    """The secrets the manager uses, with redaction of every known value."""

    def __init__(self, backend: SecretsBackend) -> None:
        self.backend = backend
        self._known: set[str] = set()
        self._lock = threading.Lock()

    def _remember(self, value: str | None) -> str | None:
        if value:
            with self._lock:
                self._known.add(value)
        return value

    def get(self, name: SecretName) -> str | None:
        return self._remember(self.backend.get(name))

    def set(self, name: SecretName, value: str) -> None:
        self.backend.set(name, value)
        self._remember(value)

    def delete(self, name: SecretName) -> None:
        self.backend.delete(name)

    def has(self, name: SecretName) -> bool:
        return self.get(name) is not None

    def generate(self, name: SecretName, prefix: str = "sk-splash-") -> str:
        value = prefix + _stdlib_secrets.token_urlsafe(32)
        self.set(name, value)
        return value

    def known_values(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._known)

    def redact(self, text: str) -> str:
        return redact_text(text, self.known_values())


def generate_internal_key() -> str:
    """The per-start key the manager gives the engine (SPEC §4.3); kept in memory only."""
    return "splash-internal-" + _stdlib_secrets.token_urlsafe(32)


_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|authorization|cookie|session)", re.IGNORECASE
)
_TOKEN_PATTERNS = (
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"splash-internal-[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
)


def redact_text(text: str, known: Iterable[str] = ()) -> str:
    """Replace known secret values and token-shaped strings with a marker."""
    for value in sorted({v for v in known if v and len(v) >= 4}, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def redact_mapping(value: Any, known: Iterable[str] = ()) -> Any:
    """A deep copy of `value` with secret-named keys and known secret values redacted."""
    known = tuple(known)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key) and isinstance(item, str):
                out[key] = REDACTED if item else item
            else:
                out[key] = redact_mapping(item, known)
        return out
    if isinstance(value, list | tuple):
        return [redact_mapping(item, known) for item in value]
    if isinstance(value, str):
        return redact_text(value, known)
    return value

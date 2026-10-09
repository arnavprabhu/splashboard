"""Secrets (SPEC §17.2): the API key, HF token override and admin session secret.

Production uses the macOS Keychain through the `security` CLI (generic passwords).
`SPLASH_GUI_SECRETS=memory|file|keychain` selects a backend; tests use `memory`.
Secrets never go into settings.json and are redacted from logs and exports.
"""

from __future__ import annotations

import base64
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


# The Keychain service prefix (D62: it follows the bundle id). The placeholder `ai.splashgui` is
# in LEGACY_PREFIXES, so the first start after the switch copies every item over
# (`migrate_prefix`, PKG-16). packaging/identity.env names the same prefix.
KEYCHAIN_PREFIX = "io.github.arnavprabhu.splashboard"
LEGACY_PREFIXES: tuple[str, ...] = ("ai.splashgui",)


class SecretName(StrEnum):
    API_KEY = f"{KEYCHAIN_PREFIX}.apikey"
    HF_TOKEN = f"{KEYCHAIN_PREFIX}.hf"  # (a Keychain service name)
    SESSION = f"{KEYCHAIN_PREFIX}.session"
    CODEX_ROUTER = f"{KEYCHAIN_PREFIX}.codexrouter"  # D58: the Codex app's router token


def suffix_of(name: str) -> str:
    """`apikey` for `<prefix>.apikey`: the part of a service name after the prefix."""
    return name.removeprefix(KEYCHAIN_PREFIX + ".")


def migrate_prefix(
    backend: SecretsBackend, suffixes: list[str], old: str, new: str
) -> dict[str, list[str]]:
    """Moves each `<old>.<suffix>` Keychain item to `<new>.<suffix>` (PKG-16, D62).

    An item is copied raw (text values stay base64), read back, and the old one is deleted only
    when the read-back matches. An item that already exists under the new name is left alone,
    and so is its old copy, so nothing is ever lost. Returns the suffixes moved, kept (both
    exist) and failed (the copy did not read back)."""
    report: dict[str, list[str]] = {"moved": [], "kept": [], "failed": []}
    for suffix in suffixes:
        value = backend.get(f"{old}.{suffix}")
        if value is None:
            continue
        target = f"{new}.{suffix}"
        if backend.get(target) is not None:
            report["kept"].append(suffix)
            continue
        try:
            backend.set(target, value)
            copied = backend.get(target)
        except SecretsError:
            copied = None
        if copied != value:
            report["failed"].append(suffix)
            continue
        backend.delete(f"{old}.{suffix}")
        report["moved"].append(suffix)
    return report


class SecretsError(RuntimeError):
    pass


class SecretsBackend(Protocol):
    name: str

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


def _check_value(value: str) -> str:
    # Visible ASCII only, as Splash's validate_api_key requires for keys; quotes and
    # backslashes are refused because `security -i` parses its input line.
    if not value or any(ord(c) <= 32 or ord(c) >= 127 or c in '"\\' for c in value):
        raise SecretsError("secret must be visible ASCII without spaces, quotes or backslashes")
    return value


class MemoryBackend:
    name = "memory"

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> str | None:
        with self._lock:
            return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        with self._lock:
            self._values[name] = _check_value(value)

    def delete(self, name: str) -> None:
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

    def get(self, name: str) -> str | None:
        with self._lock:
            return self._read().get(str(name))

    def set(self, name: str, value: str) -> None:
        with self._lock:
            data = self._read()
            data[str(name)] = _check_value(value)
            write_atomic(self.path, json.dumps(data, indent=2).encode())

    def delete(self, name: str) -> None:
        with self._lock:
            data = self._read()
            if data.pop(str(name), None) is not None:
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

    def get(self, name: str) -> str | None:
        result = self._run(["find-generic-password", "-s", str(name), "-a", self.account, "-w"])
        if result.returncode == self.NOT_FOUND:
            return None
        if result.returncode != 0:
            raise SecretsError(f"keychain read failed: {result.stderr.strip()}")
        return result.stdout.rstrip("\n") or None

    def set(self, name: str, value: str) -> None:
        value = _check_value(value)
        command = (
            f'add-generic-password -U -s "{name}" -a "{self.account}" '
            f'-l "Splashboard" -w "{value}"\n'
        )
        result = self._run(["-i"], stdin=command)
        if result.returncode != 0:
            raise SecretsError(f"keychain write failed: {result.stderr.strip()}")

    def delete(self, name: str) -> None:
        result = self._run(["delete-generic-password", "-s", str(name), "-a", self.account])
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

    def get(self, name: str) -> str | None:
        return self._remember(self.backend.get(name))

    def set(self, name: str, value: str) -> None:
        self.backend.set(name, value)
        self._remember(value)

    def delete(self, name: str) -> None:
        self.backend.delete(name)

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def generate(self, name: str, prefix: str = "sk-splash-") -> str:
        value = prefix + _stdlib_secrets.token_urlsafe(32)
        self.set(name, value)
        return value

    # Arbitrary text (MCP env/header values may hold spaces or quotes, which the
    # `security -i` command line can't carry) is stored base64-encoded.
    TEXT_PREFIX = "b64:"

    def set_text(self, name: str, value: str) -> None:
        encoded = base64.urlsafe_b64encode(value.encode()).decode()
        self.backend.set(name, self.TEXT_PREFIX + encoded)
        self._remember(value)

    def get_text(self, name: str) -> str | None:
        raw = self.backend.get(name)
        if raw is None:
            return None
        if raw.startswith(self.TEXT_PREFIX):
            try:
                raw = base64.urlsafe_b64decode(raw[len(self.TEXT_PREFIX) :]).decode()
            except (ValueError, UnicodeDecodeError):
                raise SecretsError(f"unreadable secret {name}") from None
        return self._remember(raw)

    def known_values(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._known)

    def redact(self, text: str) -> str:
        return redact_text(text, self.known_values())


def mask_secret(value: str) -> tuple[str | None, str | None, str]:
    """(prefix, last4, masked) for display (SPEC S3-23): the prefix runs to the last
    "-" in the first 11 characters (`sk-splash-`), else 4 characters; a value under
    12 characters shows nothing of itself."""
    if len(value) < 12:
        return None, None, "••••"
    prefix = value[: value.rindex("-", 0, 11) + 1] if "-" in value[:11] else value[:4]
    if len(prefix) > 10:
        prefix = value[:4]
    return prefix, value[-4:], f"{prefix}••••{value[-4:]}"


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
    # The Codex router token in its URL path (D58).
    re.compile(r"(/api/codex/t/)[^/\s\"']+"),
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

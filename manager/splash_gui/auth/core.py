"""Admin sessions and the CLI token (SPEC §8.2 security.admin_requires_key, §14, §17).

- **Session cookie** `splash_gui_session`: `v1.<expiry>.<nonce>.<signature>`, signed with
  HMAC-SHA256 under the session secret (Keychain `ai.splashgui.session`) and bound to a
  fingerprint of the current API key, so rotating the key logs every browser out.
  Sessions survive manager restarts; logout revokes the nonce until it would expire.
- **CLI token** `~/.splash/run/cli.token` (0600): a random token the CLI shim and the
  menu bar app send as `Authorization: Bearer <token>` from loopback.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import logging
import secrets as _stdlib_secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from ..paths import FILE_MODE, Paths, write_atomic
from ..secrets import SecretName, SecretsError, SecretStore

log = logging.getLogger(__name__)

SESSION_COOKIE = "splash_gui_session"
SESSION_TTL_S = 12 * 3600
LOGIN_WINDOW_S = 60.0
LOGIN_MAX_FAILURES = 10
_VERSION = "v1"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _token() -> str:
    return _stdlib_secrets.token_urlsafe(32)


@dataclass
class AuthManager:
    paths: Paths
    secrets: SecretStore
    _cli_token: str | None = None
    _session_secret: bytes | None = None
    _revoked: dict[str, float] = field(default_factory=dict)
    _failures: deque[float] = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # CLI token ---------------------------------------------------------------

    def ensure_cli_token(self) -> str:
        """Read the token file, creating (or repairing) it with mode 0600."""
        with self._lock:
            path = self.paths.cli_token
            token: str | None = None
            with contextlib.suppress(OSError, UnicodeDecodeError):
                token = path.read_text(encoding="ascii").strip() or None
            if token is None or len(token) < 32 or not token.isascii():
                token = _token()
                write_atomic(path, (token + "\n").encode("ascii"), FILE_MODE)
            elif path.stat().st_mode & 0o777 != FILE_MODE:
                path.chmod(FILE_MODE)
            self._cli_token = token
            return token

    def cli_token(self) -> str:
        return self._cli_token or self.ensure_cli_token()

    def check_cli_token(self, value: str | None) -> bool:
        if not value:
            return False
        return hmac.compare_digest(value.encode(), self.cli_token().encode())

    # API key -----------------------------------------------------------------

    def check_api_key(self, value: str) -> bool:
        try:
            key = self.secrets.get(SecretName.API_KEY)
        except SecretsError:
            return False
        return bool(key) and hmac.compare_digest(value.encode(), str(key).encode())

    def _key_fingerprint(self) -> str | None:
        try:
            key = self.secrets.get(SecretName.API_KEY)
        except SecretsError:
            return None
        return hashlib.sha256(key.encode()).hexdigest()[:32] if key else None

    # Sessions ----------------------------------------------------------------

    def _secret(self) -> bytes:
        if self._session_secret is not None:
            return self._session_secret
        value: str | None = None
        try:
            value = self.secrets.get(SecretName.SESSION) or self.secrets.generate(
                SecretName.SESSION, prefix=""
            )
        except SecretsError as error:
            # Sessions then last only as long as this process.
            log.warning("session secret unavailable (%s); using a per-process secret", error)
        self._session_secret = (value or _token()).encode()
        return self._session_secret

    def _sign(self, payload: str, fingerprint: str) -> str:
        mac = hmac.new(self._secret(), f"{payload}.{fingerprint}".encode(), hashlib.sha256)
        return _b64(mac.digest())

    def issue_session(self, now: float | None = None) -> str:
        fingerprint = self._key_fingerprint()
        if fingerprint is None:
            raise ValueError("no API key: sessions need one")
        expiry = int((time.time() if now is None else now) + SESSION_TTL_S)
        payload = f"{_VERSION}.{expiry}.{_token()}"
        return f"{payload}.{self._sign(payload, fingerprint)}"

    def _parse(self, cookie: str | None, now: float) -> tuple[str, float] | None:
        """(nonce, expiry) of a valid, unexpired, unrevoked session cookie."""
        if not cookie:
            return None
        parts = cookie.split(".")
        if len(parts) != 4 or parts[0] != _VERSION:
            return None
        version, expiry_text, nonce, signature = parts
        fingerprint = self._key_fingerprint()
        if fingerprint is None or not expiry_text.isdigit():
            return None
        expected = self._sign(f"{version}.{expiry_text}.{nonce}", fingerprint)
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            return None
        expiry = float(expiry_text)
        if expiry <= now:
            return None
        with self._lock:
            if nonce in self._revoked:
                return None
        return nonce, expiry

    def verify_session(self, cookie: str | None, now: float | None = None) -> bool:
        return self._parse(cookie, time.time() if now is None else now) is not None

    def revoke_session(self, cookie: str | None, now: float | None = None) -> None:
        now = time.time() if now is None else now
        parsed = self._parse(cookie, now)
        with self._lock:
            self._revoked = {n: e for n, e in self._revoked.items() if e > now}
            if parsed is not None:
                self._revoked[parsed[0]] = parsed[1]

    # Login throttling ----------------------------------------------------------

    def login_allowed(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            while self._failures and now - self._failures[0] > LOGIN_WINDOW_S:
                self._failures.popleft()
            return len(self._failures) < LOGIN_MAX_FAILURES

    def record_login_failure(self, now: float | None = None) -> None:
        with self._lock:
            self._failures.append(time.monotonic() if now is None else now)

"""Admin sessions and the CLI token (`security.admin_requires_key`).

- **Session cookie** `splash_gui_session`: `v1.<expiry>.<nonce>.<signature>`, signed with
  HMAC-SHA256 under the session secret (Keychain `<prefix>.session`) and bound to a
  fingerprint of the current API key, so rotating the key logs every browser out.
  Sessions survive manager restarts; logout revokes the nonce until it would expire,
  and the revocation is kept in `run/revoked-sessions.json` so it survives too.
- **CLI token** `~/.splash/run/cli.token` (0600): a random token the CLI shim and the
  menu bar app send as `Authorization: Bearer <token>` from loopback.
- **One-time login codes**: `POST /auth/link` (CLI token) mints a code, valid
  60 s and single-use, that `POST /auth/exchange` turns into a session. In memory only.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
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
LINK_TTL_S = 60
_VERSION = "v1"
_REVOKED_VERSION = 1


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
    _revoked: dict[str, float] | None = None  # loaded from disk on first use
    _codes: dict[str, float] = field(default_factory=dict)  # code -> monotonic expiry
    # Failed logins per client address, so one LAN host cannot lock out the others.
    _failures: dict[str, deque[float]] = field(default_factory=dict)
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

    def ensure_api_key(self) -> bool:
        """Sign-in uses the API key, so one exists from the first start.
        Returns whether a key was created. A Keychain failure is logged, not raised:
        the CLI token still works, and the login page explains the missing key."""
        try:
            if self.secrets.has(SecretName.API_KEY):
                return False
            self.secrets.generate(SecretName.API_KEY)
        except SecretsError as error:
            log.warning("could not create the API key (%s); browser sign-in needs one", error)
            return False
        log.info("created the API key (admin sign-in and the /v1 key use it)")
        return True

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
        now = time.time() if now is None else now
        with self._lock:
            self._revoked_map(now)  # a bad revocation file rotates the secret first
        expiry = int(now + SESSION_TTL_S)
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
        with self._lock:
            self._revoked_map(now)  # loaded before the signature check: see _fail_closed
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
            if nonce in self._revoked_map(now):
                return None
        return nonce, expiry

    def verify_session(self, cookie: str | None, now: float | None = None) -> bool:
        return self._parse(cookie, time.time() if now is None else now) is not None

    def revoke_session(self, cookie: str | None, now: float | None = None) -> None:
        now = time.time() if now is None else now
        parsed = self._parse(cookie, now)
        with self._lock:
            revoked = {n: e for n, e in self._revoked_map(now).items() if e > now}
            if parsed is not None:
                revoked[parsed[0]] = parsed[1]
            self._revoked = revoked
            self._save_revoked(revoked)

    # Revocations on disk -------------------------------------------------

    def _revoked_map(self, now: float) -> dict[str, float]:
        """Call with the lock held."""
        if self._revoked is None:
            self._revoked = self._load_revoked(now)
        return self._revoked

    def _load_revoked(self, now: float) -> dict[str, float]:
        path = self.paths.revoked_sessions
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            return self._fail_closed(f"unreadable ({error})")
        entries = data.get("revoked") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return self._fail_closed("no revoked map")
        revoked = {
            str(nonce): float(expiry)
            for nonce, expiry in entries.items()
            if isinstance(expiry, (int, float)) and expiry > now
        }
        if len(revoked) != len(entries):
            self._save_revoked(revoked)
        return revoked

    def _fail_closed(self, reason: str) -> dict[str, float]:
        """A revocation file we cannot read may have held logouts, and ignoring it
        would bring those cookies back. Rotating the session secret ends every
        session instead (browsers sign in again; the CLI token is unaffected), then a
        fresh, empty file replaces the bad one. Call with the lock held."""
        log.warning(
            "%s is %s; signing every browser out so no revoked session comes back",
            self.paths.revoked_sessions.name,
            reason,
        )
        value: str | None = None
        try:
            value = self.secrets.generate(SecretName.SESSION, prefix="")
        except SecretsError as error:
            log.warning("could not rotate the session secret (%s); using a per-process one", error)
        self._session_secret = (value or _token()).encode()
        self._save_revoked({})
        return {}

    def _save_revoked(self, revoked: dict[str, float]) -> None:
        payload = {"version": _REVOKED_VERSION, "revoked": revoked}
        try:
            write_atomic(
                self.paths.revoked_sessions,
                (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
                FILE_MODE,
            )
        except OSError as error:
            log.warning("could not save session revocations (%s)", error)

    # One-time login codes ------------------------------------------------

    def issue_code(self, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        code = _token()
        with self._lock:
            self._codes = {c: e for c, e in self._codes.items() if e > now}
            self._codes[code] = now + LINK_TTL_S
        return code

    def redeem_code(self, code: str, now: float | None = None) -> bool:
        """True once for a live code; the code is gone afterwards either way."""
        now = time.monotonic() if now is None else now
        with self._lock:
            match = next(
                (c for c in self._codes if hmac.compare_digest(c.encode(), code.encode())),
                None,
            )
            if match is None:
                return False
            expiry = self._codes.pop(match)
            return expiry > now

    # Login throttling ----------------------------------------------------------

    def login_allowed(self, client: str = "", now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            for key in list(self._failures):
                times = self._failures[key]
                while times and now - times[0] > LOGIN_WINDOW_S:
                    times.popleft()
                if not times:
                    del self._failures[key]
            return len(self._failures.get(client, ())) < LOGIN_MAX_FAILURES

    def record_login_failure(self, client: str = "", now: float | None = None) -> None:
        with self._lock:
            self._failures.setdefault(client, deque()).append(
                time.monotonic() if now is None else now
            )

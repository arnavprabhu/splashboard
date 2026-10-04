"""Secret backends and redaction (SPEC §17.2)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from splash_gui.logging_setup import RedactingFilter, setup_logging
from splash_gui.paths import Paths
from splash_gui.secrets import (
    REDACTED,
    FileBackend,
    KeychainBackend,
    MemoryBackend,
    SecretName,
    SecretsError,
    SecretStore,
    backend_from_env,
    generate_internal_key,
    redact_mapping,
    redact_text,
)

from .conftest import mode, write_script


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("memory", MemoryBackend),
        ("file", FileBackend),
        ("keychain", KeychainBackend),
        ("KEYCHAIN", KeychainBackend),
    ],
)
def test_backend_selection(paths: Paths, value: str, kind: type) -> None:
    assert isinstance(backend_from_env(paths, {"SPLASH_GUI_SECRETS": value}), kind)


def test_default_backend_on_macos(paths: Paths) -> None:
    import sys

    expected = KeychainBackend if sys.platform == "darwin" else FileBackend
    assert isinstance(backend_from_env(paths, {}), expected)


def test_unknown_backend(paths: Paths) -> None:
    with pytest.raises(SecretsError):
        backend_from_env(paths, {"SPLASH_GUI_SECRETS": "vault"})


def test_env_fixture_selects_memory(paths: Paths) -> None:
    assert isinstance(backend_from_env(paths), MemoryBackend)


def test_memory_backend_roundtrip() -> None:
    store = SecretStore(MemoryBackend())
    assert store.get(SecretName.API_KEY) is None
    key = store.generate(SecretName.API_KEY)
    assert key.startswith("sk-splash-") and store.get(SecretName.API_KEY) == key
    assert store.has(SecretName.API_KEY)
    store.delete(SecretName.API_KEY)
    assert not store.has(SecretName.API_KEY)


def test_file_backend_is_private(paths: Paths) -> None:
    backend = FileBackend(paths.run_dir / "secrets.json")
    backend.set(SecretName.HF_TOKEN, "hf_token_value")
    assert mode(backend.path) == 0o600
    assert json.loads(backend.path.read_text()) == {"ai.splashgui.hf": "hf_token_value"}
    assert backend.get(SecretName.HF_TOKEN) == "hf_token_value"
    backend.delete(SecretName.HF_TOKEN)
    assert backend.get(SecretName.HF_TOKEN) is None


@pytest.mark.parametrize("bad", ["", "has space", 'quo"te', "back\\slash", "nl\n"])
def test_secret_values_are_checked(bad: str) -> None:
    with pytest.raises(SecretsError):
        MemoryBackend().set(SecretName.API_KEY, bad)


@pytest.fixture
def fake_security(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in for /usr/bin/security that logs argv and stdin and keeps a store."""
    log = tmp_path / "security.log"
    store = tmp_path / "store"
    script = write_script(
        tmp_path / "security",
        f"""
echo "ARGV $*" >> "{log}"
case "$1" in
  -i) read -r line; echo "STDIN $line" >> "{log}";
      echo "$line" | sed -E 's/.*-w "([^"]*)".*/\\1/' > "{store}"; exit 0;;
  find-generic-password) [ -f "{store}" ] || exit 44; cat "{store}"; exit 0;;
  delete-generic-password) [ -f "{store}" ] || exit 44; rm "{store}"; exit 0;;
esac
exit 1
""",
    )
    return script, log


def test_keychain_backend_never_puts_secrets_on_argv(fake_security: tuple[Path, Path]) -> None:
    script, log = fake_security
    backend = KeychainBackend(security=str(script))
    assert backend.get(SecretName.API_KEY) is None
    backend.set(SecretName.API_KEY, "sk-splash-secret123")
    assert backend.get(SecretName.API_KEY) == "sk-splash-secret123"
    backend.delete(SecretName.API_KEY)
    backend.delete(SecretName.API_KEY)  # not found is fine
    lines = log.read_text().splitlines()
    argv_lines = [line for line in lines if line.startswith("ARGV")]
    assert all("sk-splash-secret123" not in line for line in argv_lines)
    assert any(
        line.startswith('STDIN add-generic-password -U -s "ai.splashgui.apikey"') for line in lines
    )
    assert any(
        "find-generic-password -s ai.splashgui.apikey -a splash-gui -w" in line
        for line in argv_lines
    )


def test_keychain_errors_are_wrapped(tmp_path: Path) -> None:
    backend = KeychainBackend(security=str(tmp_path / "missing"))
    with pytest.raises(SecretsError):
        backend.get(SecretName.API_KEY)


def test_redaction() -> None:
    text = (
        "key=sk-splash-abcdefghijklmnopqrstuvwxyz token hf_ABCDEFGHIJKLMNOPQRSTUVWX "
        "Authorization: Bearer abc.def custom-secret-value"
    )
    out = redact_text(text, ["custom-secret-value"])
    assert "abcdefghijklmnop" not in out and "hf_ABCDEF" not in out
    assert "abc.def" not in out and "custom-secret-value" not in out
    assert out.count(REDACTED) == 4
    internal = generate_internal_key()
    assert internal not in redact_text(f"env {internal}")


def test_redact_mapping() -> None:
    data = {
        "api_key": "x1",
        "nested": {"HF_TOKEN": "y", "ok": "visible"},
        "list": ["sk-splash-abcdefghijklmnopqrstuv"],
        "count": 3,
    }
    out = redact_mapping(data)
    assert out == {
        "api_key": REDACTED,
        "nested": {"HF_TOKEN": REDACTED, "ok": "visible"},
        "list": [REDACTED],
        "count": 3,
    }


def test_logs_are_redacted(paths: Paths) -> None:
    store = SecretStore(MemoryBackend())
    secret = store.generate(SecretName.API_KEY)
    setup_logging(paths, known_secrets=store.known_values)
    logging.getLogger("splash_gui.test").warning("leaked %s here", secret)
    for handler in logging.getLogger("splash_gui").handlers:
        handler.flush()
    text = paths.manager_log.read_text()
    assert secret not in text and REDACTED in text
    assert mode(paths.manager_log) == 0o600


def test_redacting_filter_passes_clean_records() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    assert RedactingFilter().filter(record) and record.getMessage() == "hello world"

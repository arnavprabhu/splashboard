"""Moving Keychain items to a new service prefix: copy, read back, then delete the
old item. Runs on the in-memory backend; nothing touches the real Keychain."""

from __future__ import annotations

import pytest

from splash_gui import app as app_module
from splash_gui import secrets as secrets_module
from splash_gui.app import AppConfig, create_app
from splash_gui.paths import Paths
from splash_gui.secrets import MemoryBackend, SecretStore, migrate_prefix

OLD = "ai.splashgui"
NEW = "io.github.arnavprabhu.splashboard"


class LossyBackend(MemoryBackend):
    """Accepts writes under the new prefix but never returns them: the read-back fails."""

    def get(self, name: str) -> str | None:
        if name.startswith(NEW + "."):
            return None
        return super().get(name)


def test_items_move_and_the_old_ones_go() -> None:
    backend = MemoryBackend()
    backend.set(f"{OLD}.apikey", "sk-splash-abc")
    backend.set(f"{OLD}.mcp.0123", "dGV4dA==")

    report = migrate_prefix(backend, ["apikey", "hf", "mcp.0123"], OLD, NEW)

    assert report == {"moved": ["apikey", "mcp.0123"], "kept": [], "failed": []}
    assert backend.get(f"{NEW}.apikey") == "sk-splash-abc"
    assert backend.get(f"{NEW}.mcp.0123") == "dGV4dA=="  # copied raw: text stays base64
    assert backend.get(f"{OLD}.apikey") is None and backend.get(f"{OLD}.mcp.0123") is None


def test_a_copy_that_does_not_read_back_keeps_the_old_item() -> None:
    backend = LossyBackend()
    backend.set(f"{OLD}.hf", "hf_token")

    report = migrate_prefix(backend, ["hf"], OLD, NEW)

    assert report["failed"] == ["hf"]
    assert backend.get(f"{OLD}.hf") == "hf_token"


def test_an_item_already_under_the_new_name_wins_and_the_old_copy_stays() -> None:
    backend = MemoryBackend()
    backend.set(f"{OLD}.session", "old")
    backend.set(f"{NEW}.session", "new")

    report = migrate_prefix(backend, ["session"], OLD, NEW)

    assert report["kept"] == ["session"]
    assert backend.get(f"{NEW}.session") == "new" and backend.get(f"{OLD}.session") == "old"


def test_the_manager_moves_items_at_startup_once_a_legacy_prefix_is_named(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = MemoryBackend()
    backend.set(f"{OLD}.apikey", "sk-splash-keep-me")
    monkeypatch.setattr(secrets_module, "KEYCHAIN_PREFIX", NEW)
    monkeypatch.setattr(secrets_module, "LEGACY_PREFIXES", (OLD,))

    # The move runs while the app is built, before ensure_api_key could create a new key.
    create_app(AppConfig(paths=paths, secrets=SecretStore(backend)))

    assert backend.get(f"{NEW}.apikey") == "sk-splash-keep-me"
    assert backend.get(f"{OLD}.apikey") is None


def test_without_a_legacy_prefix_nothing_is_touched(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = MemoryBackend()
    backend.set(f"{OLD}.apikey", "sk-splash-abc")
    monkeypatch.setattr(secrets_module, "LEGACY_PREFIXES", ())
    application = create_app(AppConfig(paths=paths, secrets=SecretStore(backend)))

    app_module._migrate_keychain_prefix(application.state.manager)

    assert backend.get(f"{OLD}.apikey") == "sk-splash-abc"

"""Settings: model, metadata, validation, effective merge and persistence (SPEC §8, §15.1)."""

from .effective import BUILTIN_PROFILES, effective_profiles, effective_serve, effective_values
from .model import SETTINGS_VERSION, SettingsDocument
from .store import SettingsStore
from .validation import ValidationContext, validate_document

__all__ = [
    "BUILTIN_PROFILES",
    "SETTINGS_VERSION",
    "SettingsDocument",
    "SettingsStore",
    "ValidationContext",
    "effective_profiles",
    "effective_serve",
    "effective_values",
    "validate_document",
]

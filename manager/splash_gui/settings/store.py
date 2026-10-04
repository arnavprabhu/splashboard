"""settings.json persistence (SPEC §15.1): versioned, migrated, atomic, mode 0600."""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..paths import FILE_MODE, Paths, expand, tmp_dir_for, write_atomic
from .effective import effective_values
from .metadata import FIELDS_BY_KEY, Applies
from .model import SETTINGS_VERSION, SettingsDocument
from .validation import Issue, ValidationContext, ValidationResult, validate_document

log = logging.getLogger(__name__)

Migration = Callable[[dict[str, Any]], dict[str, Any]]


def _v0_to_v1(data: dict[str, Any]) -> dict[str, Any]:
    """Pre-release files had no version; nothing else changed."""
    data["version"] = 1
    return data


MIGRATIONS: dict[int, Migration] = {0: _v0_to_v1}


class SettingsError(RuntimeError):
    pass


class SettingsReadOnlyError(SettingsError):
    """settings.json was written by a newer Splash GUI; saving would lose its fields."""


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Shape fixes that apply at any version.

    SPEC §15.1 shows `serve.offline`, while §8.2/Appendix A name `hf.offline`; the
    canonical key is `hf.offline` and a stray `serve.offline` is folded into it.
    """
    glob = data.get("global")
    if isinstance(glob, dict):
        serve = glob.get("serve")
        if isinstance(serve, dict) and "offline" in serve:
            offline = bool(serve.pop("offline"))
            hf = glob.setdefault("hf", {})
            if isinstance(hf, dict):
                hf["offline"] = bool(hf.get("offline", False)) or offline
    return data


def migrate(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Apply migrations up to SETTINGS_VERSION. Returns (data, newer_than_supported)."""
    data = copy.deepcopy(data)
    version = data.get("version", 0)
    if not isinstance(version, int) or version < 0:
        version = 0
    if version > SETTINGS_VERSION:
        return normalize(data), True
    while version < SETTINGS_VERSION:
        data = MIGRATIONS[version](data)
        version = data["version"]
    return normalize(data), False


def _delete_at(data: Any, loc: tuple[Any, ...]) -> bool:
    """Remove the value at `loc` (or its nearest dict ancestor entry)."""
    for depth in range(len(loc), 0, -1):
        parent: Any = data
        try:
            for part in loc[: depth - 1]:
                parent = parent[part]
        except (KeyError, IndexError, TypeError):
            continue
        key = loc[depth - 1]
        if isinstance(parent, dict) and key in parent:
            del parent[key]
            return True
        if isinstance(parent, list) and isinstance(key, int) and 0 <= key < len(parent):
            del parent[key]
            return True
    return False


def repair(data: dict[str, Any], limit: int = 200) -> tuple[SettingsDocument, list[str]]:
    """Validate leniently: drop each invalid or unknown value (falling back to its
    default) instead of discarding the whole file. Returns the document and what was dropped."""
    data = copy.deepcopy(data)
    dropped: list[str] = []
    for _ in range(limit):
        try:
            return SettingsDocument.model_validate(data), dropped
        except ValidationError as error:
            progressed = False
            for item in error.errors():
                loc = tuple(item["loc"])
                if _delete_at(data, loc):
                    dropped.append(f"{'.'.join(map(str, loc))}: {item['msg']}")
                    progressed = True
                    break
            if not progressed:
                break
    return SettingsDocument(), [*dropped, "settings reset to defaults"]


@dataclass(frozen=True)
class Change:
    key: str
    model: str | None
    applies: Applies
    old: Any
    new: Any


def diff(old: SettingsDocument, new: SettingsDocument) -> list[Change]:
    """Changed effective values, globally and for each model either document mentions."""
    changes: list[Change] = []
    before, after = effective_values(old), effective_values(new)
    for key, value in after.items():
        if FIELDS_BY_KEY[key].scope == "M":
            continue
        if before[key].value != value.value:
            changes.append(
                Change(key, None, FIELDS_BY_KEY[key].applies, before[key].value, value.value)
            )
    for model_id in sorted(set(old.models) | set(new.models)):
        b, a = effective_values(old, model_id), effective_values(new, model_id)
        for key in a:
            meta = FIELDS_BY_KEY[key]
            if meta.scope == "G" or not key.startswith("serve."):
                continue
            if b[key].value != a[key].value and (
                b[key].overridden_in_model or a[key].overridden_in_model
            ):
                changes.append(Change(key, model_id, meta.applies, b[key].value, a[key].value))
    return changes


class SettingsStore:
    """Thread-safe owner of settings.json."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self._lock = threading.RLock()
        self._doc: SettingsDocument | None = None
        self.read_only = False
        self.load_warnings: list[str] = []

    @property
    def file(self) -> Path:
        return self.paths.settings_file

    def load(self) -> SettingsDocument:
        with self._lock:
            self.load_warnings = []
            self.read_only = False
            try:
                text = self.file.read_text(encoding="utf-8")
            except FileNotFoundError:
                self._doc = SettingsDocument()
                return self._doc
            if self.file.stat().st_mode & 0o777 != FILE_MODE:
                self.file.chmod(FILE_MODE)
            try:
                raw = json.loads(text)
                if not isinstance(raw, dict):
                    raise ValueError("expected a JSON object")
            except ValueError as error:
                backup = self.file.with_name(f"settings.json.invalid-{int(time.time())}")
                self.file.rename(backup)
                self.load_warnings.append(f"unreadable settings moved to {backup.name}: {error}")
                log.error("settings.json unreadable (%s); moved to %s", error, backup)
                self._doc = SettingsDocument()
                return self._doc
            data, newer = migrate(raw)
            if newer:
                self.read_only = True
                self.load_warnings.append(
                    f"settings.json version {raw.get('version')} is newer than this Splash GUI "
                    f"({SETTINGS_VERSION}); settings are read-only"
                )
            doc, dropped = repair(data)
            for message in dropped:
                log.warning("settings.json: dropped %s", message)
            self.load_warnings.extend(f"dropped {m}" for m in dropped)
            self._doc = doc
            if raw.get("version") != SETTINGS_VERSION and not newer:
                self._write(doc)
            return doc

    @property
    def current(self) -> SettingsDocument:
        with self._lock:
            if self._doc is None:
                return self.load()
            return self._doc

    def _write(self, doc: SettingsDocument) -> None:
        payload = json.dumps(doc.to_json_dict(), indent=2, ensure_ascii=False) + "\n"
        write_atomic(self.file, payload.encode("utf-8"), FILE_MODE)

    def validate(self, raw: Any, context: ValidationContext | None = None) -> ValidationResult:
        if isinstance(raw, dict):
            raw = normalize(copy.deepcopy(raw))
            raw.setdefault("version", SETTINGS_VERSION)
        return validate_document(raw, context)

    def save(
        self, raw: Any, context: ValidationContext | None = None
    ) -> tuple[ValidationResult, list[Change]]:
        """Validate and persist a whole document. Nothing is written when it has errors."""
        with self._lock:
            if self.read_only:
                raise SettingsReadOnlyError(self.load_warnings[0] if self.load_warnings else "")
            result = self.validate(raw, context)
            if not result.ok or result.document is None:
                return result, []
            if result.document.version != SETTINGS_VERSION:
                result.errors.append(_version_issue(result.document.version))
                return result, []
            old = self.current
            self._write(result.document)
            self._doc = result.document
            return result, diff(old, result.document)

    # Resolved locations ---------------------------------------------------

    def models_dir(self) -> Path:
        configured = self.current.global_.storage.models_dir
        return expand(configured) if configured else self.paths.models_dir

    def cache_dir(self) -> Path:
        configured = self.current.global_.storage.cache_dir
        return expand(configured) if configured else self.paths.cache_dir

    def tmp_dir(self) -> Path:
        return tmp_dir_for(self.cache_dir())


def _version_issue(version: int) -> Issue:
    return Issue(("version",), f"version must be {SETTINGS_VERSION}, not {version}")

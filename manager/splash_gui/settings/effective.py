"""Effective settings = Splash default ← global ← per-model, with provenance (SPEC §8.1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from . import parsers as p
from .metadata import FIELDS, FieldMeta
from .model import (
    GlobalSettings,
    ModelServeOverrides,
    ModelSettings,
    SamplingOverlay,
    SettingsDocument,
)

Source = Literal["default", "global", "model"]

BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "default": {},
    "no-think": {"reasoning_effort": "none"},
    "deterministic": {"temperature": 0, "seed": 0},
    # Qwen's recommended non-thinking settings, which Splash's docs say must be sent.
    "qwen-nonthinking": {
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "presence_penalty": 1.5,
        "reasoning_effort": "none",
    },
}

_DEFAULT_GLOBAL = GlobalSettings()
_DEFAULT_MODEL_SERVE = ModelServeOverrides()


def get_path(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        obj = obj[part] if isinstance(obj, dict) else getattr(obj, part)
    return obj


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _same(field: FieldMeta, a: Any, b: Any) -> bool:
    """Equal as Splash would read them ("0" and "0G" are both an off SSD tier)."""
    parse = _COMPARE.get(field.key)
    if parse is not None and isinstance(a, str) and isinstance(b, str):
        try:
            return bool(parse(a) == parse(b))
        except ValueError:
            return a == b
    return bool(_jsonable(a) == _jsonable(b))


_COMPARE: dict[str, Any] = {
    "serve.max_memory": p.parse_max_memory,
    "serve.max_context": p.parse_max_context,
    "serve.max_cache_disk": p.parse_max_cache_disk,
    "serve.max_request_size": p.parse_request_size,
    "serve.idle_release": p.parse_idle_release,
}


@dataclass(frozen=True)
class EffectiveValue:
    key: str
    value: Any
    source: Source
    default: Any
    global_value: Any | None
    model_value: Any | None
    overridden_in_model: bool


def _default_for(field: FieldMeta) -> Any:
    if field.scope == "M":
        return get_path(_DEFAULT_MODEL_SERVE, field.key.removeprefix("serve."))
    return get_path(_DEFAULT_GLOBAL, field.key)


def _model_override(entry: ModelSettings | None, field: FieldMeta) -> tuple[bool, Any]:
    if entry is None or field.scope == "G" or not field.key.startswith("serve."):
        return False, None
    name = field.key.removeprefix("serve.")
    if name in entry.serve.model_fields_set:
        return True, getattr(entry.serve, name)
    return False, None


def effective_values(
    doc: SettingsDocument, model_id: str | None = None
) -> dict[str, EffectiveValue]:
    """Every settings.json field (Keychain secrets excluded), resolved for `model_id`."""
    entry = doc.models.get(model_id) if model_id else None
    out: dict[str, EffectiveValue] = {}
    for field in FIELDS:
        if field.storage != "settings":
            continue
        default = _default_for(field)
        global_value = None if field.scope == "M" else get_path(doc.global_, field.key)
        has_model, model_value = _model_override(entry, field)
        if has_model:
            value, source = model_value, "model"
        elif field.scope != "M" and not _same(field, global_value, default):
            value, source = global_value, "global"
        else:
            value, source = (default if field.scope == "M" else global_value), "default"
        out[field.key] = EffectiveValue(
            key=field.key,
            value=_jsonable(value),
            source=source,  # type: ignore[arg-type]
            default=_jsonable(default),
            global_value=_jsonable(global_value),
            model_value=_jsonable(model_value) if has_model else None,
            overridden_in_model=has_model,
        )
    return out


@dataclass(frozen=True)
class EffectiveServe:
    """The engine settings for one model, as the flag builder needs them."""

    model: str
    legacy: bool
    revision: str | None
    draft_model: str | None
    language_only: bool
    offline: bool
    served_model_names: tuple[str, ...]
    announce_served_name: bool
    default_reasoning_effort: str | None
    kv_format: str
    max_memory: str
    max_cache_disk: str
    persistent_cache: bool
    max_context: str
    decode_share: float
    max_request_size: str
    max_image_pixels: int
    request_timeout: float | None
    queue_size: int
    idle_release: str = "10m"
    disable_ane: bool = False
    allow_idle_sleep: bool = False


def effective_serve(doc: SettingsDocument, model_id: str) -> EffectiveServe:
    values = effective_values(doc, model_id)

    def v(key: str) -> Any:
        return values[key].value

    return EffectiveServe(
        model=model_id,
        legacy=p.is_legacy_package(model_id),
        revision=v("serve.revision"),
        draft_model=v("serve.draft_model"),
        language_only=bool(v("serve.language_only")),
        offline=bool(doc.global_.hf.offline),
        served_model_names=tuple(v("serve.served_model_names")),
        announce_served_name=bool(v("serve.announce_served_name")),
        default_reasoning_effort=v("serve.default_reasoning_effort"),
        kv_format=v("serve.kv_format"),
        max_memory=v("serve.max_memory"),
        max_cache_disk=v("serve.max_cache_disk"),
        persistent_cache=bool(v("serve.persistent_cache")),
        max_context=v("serve.max_context"),
        decode_share=float(v("serve.decode_share")),
        max_request_size=v("serve.max_request_size"),
        max_image_pixels=int(v("serve.max_image_pixels")),
        request_timeout=v("serve.request_timeout"),
        queue_size=int(v("serve.queue_size")),
        idle_release=str(v("serve.idle_release")),
        disable_ane=bool(v("serve.disable_ane")),
        allow_idle_sleep=bool(v("serve.allow_idle_sleep")),
    )


@dataclass(frozen=True)
class EffectiveProfile:
    name: str
    overlay: dict[str, Any]
    builtin: bool
    modified: bool


def effective_profiles(doc: SettingsDocument, model_id: str) -> dict[str, EffectiveProfile]:
    """Built-in profiles (SPEC §7.5) merged with the model's own; `null` hides a built-in."""
    entry = doc.models.get(model_id)
    stored = entry.profiles if entry else {}
    out: dict[str, EffectiveProfile] = {}
    for name, overlay in BUILTIN_PROFILES.items():
        if name in stored:
            custom = stored[name]
            if custom is None:
                continue
            out[name] = EffectiveProfile(name, _overlay_dict(custom), True, True)
        else:
            out[name] = EffectiveProfile(name, dict(overlay), True, False)
    for name, custom in stored.items():
        if name not in BUILTIN_PROFILES and custom is not None:
            out[name] = EffectiveProfile(name, _overlay_dict(custom), False, False)
    return out


def profile_reasoning_effort(
    doc: SettingsDocument, model: str | None, active: str | None = None
) -> str | None:
    """The `reasoning_effort` a `<id>:<profile>` model name asks for, or None
    (no profile, or one without an effort). The overlay is the request-level one
    the proxy would apply (the model's defaults, then the profile; §7.5). `active`
    resolves a profile on an alias of the loaded model, as the proxy does. Used by
    `splash launch` to set the client's own per-run option (D60)."""
    if not model:
        return None
    base, sep, name = model.rpartition(":")
    if not sep or not base:
        return None
    for candidate in dict.fromkeys(c for c in (base, active) if c):
        profile = effective_profiles(doc, candidate).get(name)
        if profile is not None:
            overlay = {**sampling_defaults(doc, candidate), **profile.overlay}
            effort = overlay.get("reasoning_effort")
            return str(effort) if effort else None
    return None


def _overlay_dict(overlay: SamplingOverlay) -> dict[str, Any]:
    return overlay.model_dump(mode="json", exclude_unset=True, exclude_none=True)


def sampling_defaults(doc: SettingsDocument, model_id: str) -> dict[str, Any]:
    entry = doc.models.get(model_id)
    return _overlay_dict(entry.sampling_defaults) if entry else {}

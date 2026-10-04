"""`GET /settings/schema`: the form schema from field metadata plus Splash's own help (§8.4)."""

from __future__ import annotations

from typing import Any

from ..engine.serve_options import EngineOptions
from ..schemas import (
    EngineOptionOut,
    EngineOptionsInfo,
    SchemaField,
    SchemaSection,
    SettingsSchema,
)
from .effective import get_path
from .metadata import FIELDS, SECTIONS, FieldMeta
from .model import GlobalSettings, ModelServeOverrides

LEGACY_DISABLED = frozenset({"serve.revision", "serve.draft_model", "serve.language_only"})

_DEFAULT_GLOBAL = GlobalSettings()
_DEFAULT_MODEL = ModelServeOverrides()

# Request-level fields a profile or the per-model defaults may set (SPEC §7.5, §3.4).
PROFILE_FIELDS: tuple[FieldMeta, ...] = (
    FieldMeta(
        "temperature",
        "Temperature",
        "0–2. Splash default 1.0.",
        "reasoning_sampling",
        "number",
        min=0,
        max=2,
    ),
    FieldMeta(
        "top_p",
        "Top P",
        "(0, 1]. Splash default 0.95.",
        "reasoning_sampling",
        "number",
        min=0,
        max=1,
    ),
    FieldMeta(
        "top_k",
        "Top K",
        "0 or -1 = no limit. Splash default 20.",
        "reasoning_sampling",
        "number",
        min=-1,
    ),
    FieldMeta(
        "min_p",
        "Min P",
        "0–1. Not sent to Anthropic Messages.",
        "reasoning_sampling",
        "number",
        min=0,
        max=1,
    ),
    FieldMeta(
        "presence_penalty",
        "Presence penalty",
        "−2 to 2. Not sent to Messages.",
        "reasoning_sampling",
        "number",
        min=-2,
        max=2,
    ),
    FieldMeta(
        "frequency_penalty",
        "Frequency penalty",
        "−2 to 2. Not sent to Messages.",
        "reasoning_sampling",
        "number",
        min=-2,
        max=2,
    ),
    FieldMeta(
        "repetition_penalty",
        "Repetition penalty",
        "Positive. Splash default 1. Not sent to Messages.",
        "reasoning_sampling",
        "number",
        min=0,
    ),
    FieldMeta("seed", "Seed", "Unsigned 64-bit integer.", "reasoning_sampling", "number", min=0),
    FieldMeta(
        "stop",
        "Stop sequences",
        "Up to four strings. Not sent to Messages.",
        "reasoning_sampling",
        "tags",
    ),
    FieldMeta(
        "priority",
        "Priority",
        "Scheduling priority.",
        "reasoning_sampling",
        "select",
        choices=("foreground", "normal", "background"),
    ),
    FieldMeta(
        "timeout",
        "Timeout",
        "Seconds; can only shorten --request-timeout.",
        "reasoning_sampling",
        "number",
        min=0,
        unit="s",
    ),
    FieldMeta(
        "max_tokens",
        "Max output tokens",
        "Sent as max_completion_tokens (Chat), "
        "max_tokens (Completions) or max_output_tokens (Responses); never to Messages.",
        "reasoning_sampling",
        "number",
        min=1,
    ),
    FieldMeta(
        "reasoning_effort",
        "Reasoning effort",
        "Chat reasoning_effort, Responses reasoning.effort.",
        "reasoning_sampling",
        "select",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    ),
    FieldMeta(
        "chat_template_kwargs",
        "Chat template kwargs",
        'JSON object, e.g. {"enable_thinking": false}. Chat only.',
        "reasoning_sampling",
        "json",
    ),
    FieldMeta(
        "ignore_eos",
        "Ignore EOS",
        "Chat and Completions only; not with tools or structured output.",
        "reasoning_sampling",
        "toggle",
    ),
    FieldMeta(
        "thinking",
        "Messages thinking",
        "Anthropic `thinking` object, sent to Messages only when set.",
        "reasoning_sampling",
        "json",
    ),
)


def default_for(meta: FieldMeta) -> Any:
    if meta.storage == "keychain":
        return None
    if meta.scope == "M":
        return _jsonable(get_path(_DEFAULT_MODEL, meta.key.removeprefix("serve.")))
    return _jsonable(get_path(_DEFAULT_GLOBAL, meta.key))


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _field(meta: FieldMeta, options: dict[str, EngineOptionOut], default: Any) -> SchemaField:
    return SchemaField(
        key=meta.key,
        label=meta.label,
        help=meta.help,
        section=meta.section,
        control=meta.control,
        applies=meta.applies,
        scope=meta.scope,
        flag=meta.flag,
        env=meta.env,
        advanced=meta.advanced,
        choices=list(meta.choices) or None,
        min=meta.min,
        max=meta.max,
        unit=meta.unit,
        storage=meta.storage,
        default=default,
        warnings=list(meta.warnings),
        disabled_for_legacy=meta.key in LEGACY_DISABLED,
        engine=options.get(meta.engine_option) if meta.engine_option else None,
    )


def build_schema(engine_options: EngineOptions) -> SettingsSchema:
    options = {o.flag: EngineOptionOut.model_validate(o.as_dict()) for o in engine_options.options}
    fields = [_field(meta, options, default_for(meta)) for meta in FIELDS]
    sections = [
        SchemaSection(id=s.id, label=s.label, fields=[f.key for f in FIELDS if f.section == s.id])
        for s in SECTIONS
    ]
    sections.append(
        SchemaSection(
            id="integrations",
            label="Integrations",
            fields=[f.key for f in FIELDS if f.section == "integrations"],
        )
    )
    return SettingsSchema(
        sections=sections,
        fields=fields,
        profile_fields=[_field(meta, {}, None) for meta in PROFILE_FIELDS],
        engine_options=EngineOptionsInfo(
            available=engine_options.available,
            version=engine_options.version,
            errors=list(engine_options.errors),
            unknown=[options[o.flag] for o in engine_options.unknown()],
        ),
    )

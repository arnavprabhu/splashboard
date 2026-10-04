"""The settings document (SPEC §8.2, §15.1, Appendix A) as pydantic models.

Single-field checks live here, mirroring Splash's parsers; checks that involve
more than one field (and per-model legacy rules) live in `validation.py`.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

from . import parsers as p

SETTINGS_VERSION = 1

Theme = Literal["light", "dark", "system"]
KvFormat = Literal["int8", "bf16"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
Priority = Literal["foreground", "normal", "background"]
SwitchWhenBusy = Literal["reject", "wait"]
PresetId = Literal["coding", "chat", "speed"]

CLAUDE_DESKTOP_SLOTS: tuple[str, ...] = (
    "claude-fable-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
)
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def _to_text(value: Any) -> Any:
    """Sizes and context limits may arrive as JSON numbers; store their text."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return value


SizeText = Annotated[str, BeforeValidator(_to_text)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# Global sections -------------------------------------------------------------


class ServerSettings(_Strict):
    host: str = "127.0.0.1"
    port: int = 8000
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_origins: list[str] = Field(default_factory=list)

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        return p.parse_bind_host(value)

    @field_validator("port")
    @classmethod
    def _port(cls, value: int) -> int:
        return p.parse_port(str(value))

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(p.parse_allowed_host(v) for v in value))

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, value: list[str]) -> list[str]:
        for origin in value:
            p.parse_allowed_origin(origin)
        return list(dict.fromkeys(value))


class SecuritySettings(_Strict):
    api_key_required: bool = False
    admin_requires_key: bool = False


class ExtraFlag(_Strict):
    """An engine option Appendix A does not know (SPEC §8.4). `value: null` = a bare switch."""

    flag: str
    value: str | None = None

    @field_validator("flag")
    @classmethod
    def _flag(cls, value: str) -> str:
        if not re.fullmatch(r"--[a-z0-9][a-z0-9-]*", value):
            raise ValueError("must be a long option such as --new-option")
        return value

    @field_validator("value")
    @classmethod
    def _value(cls, value: str | None) -> str | None:
        if value is not None and any(not c.isprintable() for c in value):
            raise ValueError("must not contain control characters")
        return value


class EngineSettings(_Strict):
    path: str | None = None
    internal_port: Literal["auto"] | int = "auto"
    extra_flags: list[ExtraFlag] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        return value

    @field_validator("internal_port")
    @classmethod
    def _internal_port(cls, value: Literal["auto"] | int) -> Literal["auto"] | int:
        if value == "auto":
            return value
        return p.parse_port(str(value))


class GlobalServeSettings(_Strict):
    """`splash serve` options with a global value (scope G or G/M in Appendix A)."""

    max_memory: SizeText = "auto"
    max_context: SizeText = "auto"
    kv_format: KvFormat = "int8"
    max_cache_disk: SizeText = "0"
    persistent_cache: bool = False
    decode_share: float = p.DEFAULT_DECODE_SHARE
    max_request_size: SizeText = "128M"
    max_image_pixels: int = p.MAX_IMAGE_PIXELS
    request_timeout: float | None = None
    queue_size: int = p.DEFAULT_QUEUE_SIZE
    default_reasoning_effort: ReasoningEffort | None = None

    @field_validator("max_memory")
    @classmethod
    def _max_memory(cls, value: str) -> str:
        p.parse_max_memory(value)
        return value

    @field_validator("max_context")
    @classmethod
    def _max_context(cls, value: str) -> str:
        p.parse_max_context(value)
        return value

    @field_validator("max_cache_disk")
    @classmethod
    def _max_cache_disk(cls, value: str) -> str:
        p.parse_max_cache_disk(value)
        return value

    @field_validator("max_request_size")
    @classmethod
    def _max_request_size(cls, value: str) -> str:
        p.parse_request_size(value)
        return value

    @field_validator("decode_share")
    @classmethod
    def _decode_share(cls, value: float) -> float:
        return p.parse_decode_share(repr(float(value)))

    @field_validator("max_image_pixels")
    @classmethod
    def _max_image_pixels(cls, value: int) -> int:
        return p.parse_max_image_pixels(str(value))

    @field_validator("request_timeout")
    @classmethod
    def _request_timeout(cls, value: float | None) -> float | None:
        return None if value is None else p.parse_request_timeout(repr(float(value)))

    @field_validator("queue_size")
    @classmethod
    def _queue_size(cls, value: int) -> int:
        return p.parse_queue_size(str(value))


class StorageSettings(_Strict):
    """`null` means the default under the base directory (`~/.splash/models`, `~/.splash/cache`)."""

    models_dir: str | None = None
    cache_dir: str | None = None

    @field_validator("models_dir", "cache_dir")
    @classmethod
    def _dir(cls, value: str | None) -> str | None:
        if value is None:
            return None
        p.parse_cache_dir(value)
        return value.strip()


class HfSettings(_Strict):
    endpoint: str | None = None
    offline: bool = False

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().rstrip("/")
        if not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", value):
            raise ValueError("must be an http(s) URL such as https://hf-mirror.com")
        return value


class RoutingSettings(_Strict):
    auto_load: bool = True
    switch_when_busy: SwitchWhenBusy = "reject"
    default_model: str | None = None
    unknown_model_fallback: bool = False
    load_timeout: float = 120

    @field_validator("default_model")
    @classmethod
    def _default_model(cls, value: str | None) -> str | None:
        return None if value is None else p.parse_model_id(value)

    @field_validator("load_timeout")
    @classmethod
    def _load_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("must be a positive number of seconds")
        return value


class LifecycleSettings(_Strict):
    launch_at_login: bool = True
    stop_on_quit: bool = True
    auto_restart: bool = True
    idle_unload: bool = False
    idle_unload_minutes: int = Field(default=30, ge=5, le=240)


class MenubarSettings(_Strict):
    show_tokps: bool = False
    show_memory: bool = False
    show_gpu: bool = False
    show_switcher: bool = False


class NotificationSettings(_Strict):
    download_done: bool = True
    engine_failed: bool = True
    memory_critical: bool = True
    update_available: bool = True
    disk_cache_errors: bool = True


class DownloadSettings(_Strict):
    parallel: int = Field(default=1, ge=1, le=3)
    full_verify: bool = False


class McpSecretRef(_Strict):
    """An MCP `env`/`headers` value kept in the Keychain (D43): settings.json and the
    API carry only this reference with its mask (`prefix••••last4`, S3-23)."""

    secret: Literal[True]
    masked: str


McpValue = str | McpSecretRef


class McpServer(_Strict):
    """One MCP server, in a shape compatible with `mcp.json` entries (SPEC §10.5).

    Every `env` and `headers` value is a secret (D43). A plain string is a new value
    (the manager moves it to the Keychain on save); a `{"secret": true, "masked"}`
    reference keeps the stored one."""

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, McpValue] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, McpValue] = Field(default_factory=dict)
    enabled: bool = True
    always_allow: bool = False

    @model_validator(mode="after")
    def _transport(self) -> McpServer:
        if (self.command is None) == (self.url is None):
            raise ValueError("set exactly one of command (stdio) or url (streamable HTTP)")
        return self


class ChatSettings(_Strict):
    mcp_servers: dict[str, McpServer] = Field(default_factory=dict)

    @field_validator("mcp_servers")
    @classmethod
    def _names(cls, value: dict[str, McpServer]) -> dict[str, McpServer]:
        for name in value:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
                raise ValueError(f"invalid MCP server name {name!r}")
        return value


class ClaudeDesktopSettings(_Strict):
    port: int = 18435
    # Slot -> Splash model ID or `ID:profile`; null means "the active model".
    slots: dict[str, str | None] = Field(
        default_factory=lambda: dict.fromkeys(CLAUDE_DESKTOP_SLOTS)
    )

    @field_validator("port")
    @classmethod
    def _port(cls, value: int) -> int:
        return p.parse_port(str(value))

    @field_validator("slots")
    @classmethod
    def _slots(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        unknown = set(value) - set(CLAUDE_DESKTOP_SLOTS)
        if unknown:
            raise ValueError(f"unknown Claude Desktop slot(s): {', '.join(sorted(unknown))}")
        merged = dict.fromkeys(CLAUDE_DESKTOP_SLOTS)
        merged.update(value)
        for target in merged.values():
            if target is not None and not target.strip():
                raise ValueError("a slot target must be a model ID or null")
        return merged


class CodexAppSettings(_Strict):
    make_default: bool = False


class IntegrationSettings(_Strict):
    claude_desktop: ClaudeDesktopSettings = Field(default_factory=ClaudeDesktopSettings)
    codex_app: CodexAppSettings = Field(default_factory=CodexAppSettings)


class AdvancedSettings(_Strict):
    crash_trace: bool = False


class UiSettings(_Strict):
    theme: Theme = "light"


class WizardSettings(_Strict):
    completed: bool = False
    preset: PresetId | None = None


class GlobalSettings(_Strict):
    server: ServerSettings = Field(default_factory=ServerSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    serve: GlobalServeSettings = Field(default_factory=GlobalServeSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    hf: HfSettings = Field(default_factory=HfSettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    lifecycle: LifecycleSettings = Field(default_factory=LifecycleSettings)
    menubar: MenubarSettings = Field(default_factory=MenubarSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    downloads: DownloadSettings = Field(default_factory=DownloadSettings)
    chat: ChatSettings = Field(default_factory=ChatSettings)
    integrations: IntegrationSettings = Field(default_factory=IntegrationSettings)
    advanced: AdvancedSettings = Field(default_factory=AdvancedSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    wizard: WizardSettings = Field(default_factory=WizardSettings)


# Per-model -----------------------------------------------------------------


def _drop_nulls(value: Any, keep: frozenset[str] = frozenset()) -> Any:
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if v is not None or k in keep}
    return value


def _no_schema_defaults(schema: dict[str, Any]) -> None:
    """Sparse models have no defaults on the wire: an absent key means "not set"."""
    for prop in schema.get("properties", {}).values():
        prop.pop("default", None)


class _Sparse(_Strict):
    """A per-model overlay whose keys only exist when set (SPEC §15.1, docs/api.md §6.1).

    Every dump (the API, settings.json, internal read-modify-write) carries only the
    keys that were given, so a client that saves back what it read never turns a
    default into an explicit override that shadows the global value."""

    model_config = ConfigDict(json_schema_extra=_no_schema_defaults)

    # No return annotation on purpose: pydantic would take it as the serialization
    # schema, and the OpenAPI document (and the generated web types) would lose the
    # model's fields. Without one, the JSON schema stays the model's own.
    @model_serializer(mode="wrap")
    def _only_set(self, handler: SerializerFunctionWrapHandler):  # type: ignore[no-untyped-def]
        data = handler(self)
        if not isinstance(data, dict):
            return data
        return {k: v for k, v in data.items() if k in self.model_fields_set}


class ModelServeOverrides(_Sparse):
    """Per-model engine overrides (scope M or G/M). Only keys present are overrides;
    an absent key inherits the global value. `default_reasoning_effort: null` is an
    explicit override back to the model template's default."""

    revision: str | None = None
    draft_model: str | None = None
    language_only: bool = False
    served_model_names: list[str] = Field(default_factory=list)
    announce_served_name: bool = False
    default_reasoning_effort: ReasoningEffort | None = None
    kv_format: KvFormat = "int8"
    max_context: SizeText = "auto"
    decode_share: float = p.DEFAULT_DECODE_SHARE
    max_image_pixels: int = p.MAX_IMAGE_PIXELS

    @model_validator(mode="before")
    @classmethod
    def _nulls_mean_inherit(cls, data: Any) -> Any:
        return _drop_nulls(data, keep=frozenset({"default_reasoning_effort"}))

    @field_validator("revision")
    @classmethod
    def _revision(cls, value: str | None) -> str | None:
        if value is not None and (not value or any(c.isspace() for c in value)):
            raise ValueError("must be a branch, tag or commit without spaces")
        return value

    @field_validator("draft_model")
    @classmethod
    def _draft(cls, value: str | None) -> str | None:
        # Kept as typed: Splash resolves a local directory to an absolute path itself.
        if value is not None:
            p.parse_draft_model(value)
        return value

    @field_validator("served_model_names")
    @classmethod
    def _aliases(cls, value: list[str]) -> list[str]:
        return [p.parse_served_model_name(v) for v in value]

    @field_validator("max_context")
    @classmethod
    def _max_context(cls, value: str) -> str:
        p.parse_max_context(value)
        return value

    @field_validator("decode_share")
    @classmethod
    def _decode_share(cls, value: float) -> float:
        return p.parse_decode_share(repr(float(value)))

    @field_validator("max_image_pixels")
    @classmethod
    def _max_image_pixels(cls, value: int) -> int:
        return p.parse_max_image_pixels(str(value))


class ThinkingConfig(_Strict):
    """Anthropic Messages `thinking` (server/api_shapes.py `_anthropic_thinking`)."""

    type: Literal["enabled", "disabled", "adaptive"]
    budget_tokens: int | None = Field(default=None, ge=1)
    display: Literal["summarized", "omitted", "updates"] | None = None

    @model_validator(mode="after")
    def _display(self) -> ThinkingConfig:
        if self.display is not None and self.type == "disabled":
            raise ValueError("thinking.display requires enabled or adaptive thinking")
        return self


_SAMPLING_REQUIREMENTS = {
    # server/frontend.py SAMPLING_NUMBERS: "<name> must be <requirement>"
    "temperature": "a number in [0, 2]",
    "top_p": "a number in (0, 1]",
    "presence_penalty": "a number in [-2, 2]",
    "frequency_penalty": "a number in [-2, 2]",
    "repetition_penalty": "a positive number",
    "min_p": "a number in [0, 1]",
}
_INTEGER_MESSAGES = {
    "top_k": "top_k must be 0 or -1 (disabled) or a positive integer",
    "seed": "seed must be an unsigned 64-bit integer",
    "max_tokens": "max_tokens must be a positive integer",
}


def _is_number(value: Any) -> bool:
    # server/metrics.py is_finite_number: JSON numbers only, never booleans.
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)


def _check_wire_type(name: str, value: Any) -> None:
    """Refuse what Splash refuses before pydantic would coerce it (True -> 1, "20" -> 20)."""
    if value is None:
        return
    if name in _SAMPLING_REQUIREMENTS and not _is_number(value):
        raise ValueError(f"{name} must be {_SAMPLING_REQUIREMENTS[name]}")
    if name in _INTEGER_MESSAGES and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValueError(_INTEGER_MESSAGES[name])
    if name == "timeout" and not _is_number(value):
        raise ValueError("timeout must be positive")
    if name == "ignore_eos" and not isinstance(value, bool):
        raise ValueError("ignore_eos must be a boolean")


class SamplingOverlay(_Sparse):
    """Request fields a profile or the per-model defaults inject (SPEC §7.5), with
    Splash's ranges (server/frontend.py SAMPLING_NUMBERS and friends). Absent = not set."""

    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    repetition_penalty: float | None = None
    seed: int | None = None
    stop: str | list[str] | None = None
    priority: Priority | None = None
    timeout: float | None = None
    max_tokens: int | None = None
    reasoning_effort: ReasoningEffort | None = None
    chat_template_kwargs: dict[str, Any] | None = None
    ignore_eos: bool | None = None
    thinking: ThinkingConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _nulls_are_unset(cls, data: Any) -> Any:
        return _drop_nulls(data)

    @field_validator(
        *_SAMPLING_REQUIREMENTS, *_INTEGER_MESSAGES, "timeout", "ignore_eos", mode="before"
    )
    @classmethod
    def _wire_type(cls, value: Any, info: ValidationInfo) -> Any:
        _check_wire_type(str(info.field_name), value)
        return value

    @field_validator("temperature")
    @classmethod
    def _temperature(cls, v: float | None) -> float | None:
        if v is not None and not 0 <= v <= 2:
            raise ValueError("temperature must be a number in [0, 2]")
        return v

    @field_validator("top_p")
    @classmethod
    def _top_p(cls, v: float | None) -> float | None:
        if v is not None and not p.MIN_FLOAT32_SUBNORMAL <= v <= 1:
            raise ValueError("top_p must be a number in (0, 1]")
        return v

    @field_validator("top_k")
    @classmethod
    def _top_k(cls, v: int | None) -> int | None:
        if v is not None and v < -1:
            raise ValueError("top_k must be 0 or -1 (disabled) or a positive integer")
        return v

    @field_validator("min_p")
    @classmethod
    def _min_p(cls, v: float | None) -> float | None:
        if v is not None and not 0 <= v <= 1:
            raise ValueError("min_p must be a number in [0, 1]")
        return v

    @field_validator("presence_penalty", "frequency_penalty")
    @classmethod
    def _penalty(cls, v: float | None, info: ValidationInfo) -> float | None:
        if v is not None and not -2 <= v <= 2:
            raise ValueError(f"{info.field_name} must be a number in [-2, 2]")
        return v

    @field_validator("repetition_penalty")
    @classmethod
    def _repetition(cls, v: float | None) -> float | None:
        if v is not None and not p.MIN_FLOAT32_SUBNORMAL <= v <= p.FLOAT32_MAX:
            raise ValueError("repetition_penalty must be a positive number")
        return v

    @field_validator("seed")
    @classmethod
    def _seed(cls, v: int | None) -> int | None:
        if v is not None and not 0 <= v < 2**64:
            raise ValueError("seed must be an unsigned 64-bit integer")
        return v

    @field_validator("stop")
    @classmethod
    def _stop(cls, v: str | list[str] | None) -> str | list[str] | None:
        if v is None or v == []:
            return None
        if isinstance(v, str) and v:
            return v
        if (
            isinstance(v, list)
            and 1 <= len(v) <= p.MAX_STOP_SEQUENCES
            and all(isinstance(s, str) and s for s in v)
        ):
            return v
        raise ValueError("stop must be a string or up to four strings")

    @field_validator("timeout")
    @classmethod
    def _timeout(cls, v: float | None) -> float | None:
        if v is not None and (not math.isfinite(v) or v <= 0):
            raise ValueError("timeout must be positive")
        return v

    @field_validator("max_tokens")
    @classmethod
    def _max_tokens(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("max_tokens must be a positive integer")
        return v


class ModelSettings(_Strict):
    serve: ModelServeOverrides = Field(default_factory=ModelServeOverrides)
    sampling_defaults: SamplingOverlay = Field(default_factory=SamplingOverlay)
    # Profile name -> overlay. `null` hides a built-in profile (`default` cannot be hidden).
    profiles: dict[str, SamplingOverlay | None] = Field(default_factory=dict)

    @field_validator("profiles")
    @classmethod
    def _profile_names(
        cls, value: dict[str, SamplingOverlay | None]
    ) -> dict[str, SamplingOverlay | None]:
        for name, overlay in value.items():
            if not PROFILE_NAME.fullmatch(name):
                raise ValueError(
                    f"profile name {name!r} must be 1–32 lowercase letters, digits, - or _"
                )
            if name == "default" and overlay is None:
                raise ValueError("the default profile cannot be removed")
        return value


class SettingsDocument(_Strict):
    version: int = SETTINGS_VERSION
    global_: GlobalSettings = Field(default_factory=GlobalSettings, alias="global")
    models: dict[str, ModelSettings] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", populate_by_name=True, validate_assignment=True)

    @field_validator("models")
    @classmethod
    def _model_ids(cls, value: dict[str, ModelSettings]) -> dict[str, ModelSettings]:
        for model_id in value:
            p.parse_model_id(model_id)
        return value

    def to_json_dict(self) -> dict[str, Any]:
        """The on-disk shape: full global section, sparse per-model overrides."""
        data = self.model_dump(mode="json", by_alias=True, exclude={"models"})
        data["models"] = {
            model_id: {
                "serve": entry.serve.model_dump(mode="json", exclude_unset=True),
                "sampling_defaults": entry.sampling_defaults.model_dump(
                    mode="json", exclude_unset=True
                ),
                "profiles": {
                    name: None
                    if overlay is None
                    else overlay.model_dump(mode="json", exclude_unset=True)
                    for name, overlay in entry.profiles.items()
                },
            }
            for model_id, entry in self.models.items()
        }
        return data

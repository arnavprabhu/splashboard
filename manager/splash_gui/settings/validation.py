"""Whole-document validation: field errors from the model plus the
cross-field rules Splash enforces in `check_serve_arguments` and the GUI's own."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from . import parsers as p
from .effective import effective_values
from .model import SettingsDocument

Severity = Literal["error", "warning"]

# Splash's refusal of --persistent-cache with the SSD cache off
# (serve_options.py check_serve_arguments).
PERSISTENT_NEEDS_CACHE = "--persistent-cache needs the SSD cache --max-cache-disk 0 turns off"
WRITES_NEED_CACHE = "--max-cache-disk-writes needs the SSD cache --max-cache-disk 0 turns off"
STATE_BF16_TRADEOFF = "Results differ from fp32 in the last bits"
# A context past the native window turns on YaRN (splash/DEVELOPMENT.md "Memory and
# context"); a block-sparse model's context also follows the SSD cache's volume.
YARN_TRADEOFF = (
    "Past 256K Splash turns on YaRN, which is static: it changes answers at every "
    "length, and each such context keeps its own persistent cache. A "
    "Qwen3.8-Flash-Next model's context also follows the free space of the SSD "
    "cache's disk"
)
# Splash ignores --sparse-kv-memory without the SSD cache, where no KV spills.
SPARSE_KV_NEEDS_CACHE = "Ignored with the SSD cache off: no KV goes to the SSD"

# One state's size: a quota below it leaves the SSD cache off (splash/DEVELOPMENT.md
# "SSD cache"). The docs give no size for Qwen3.8-Flash-Next's state.
MIN_USEFUL_CACHE_DISK = {"Qwen3.6-35B-A3B": 64 * 1024**2, "Qwen3.8-27B": 153 * 1024**2}

# Flags the GUI sets itself; `engine.extra_flags` may not repeat them.
MANAGED_FLAGS = frozenset(
    {
        "--model",
        "--port",
        "--host",
        "--no-webui",
        "--api-key",
        "--revision",
        "--draft-model",
        "--language-only",
        "--offline",
        "--served-model-name",
        "--announce-served-name",
        "--default-reasoning-effort",
        "--thinking-token-budget",
        "--thinking-end-text",
        "--kv-format",
        "--state-format",
        "--expert-memory",
        "--ple-memory",
        "--sparse-kv-memory",
        "--verify-rows",
        "--max-memory",
        "--max-cache-disk",
        "--max-cache-disk-writes",
        "--persistent-cache",
        "--cache-dir",
        "--max-context",
        "--decode-share",
        "--allowed-host",
        "--allowed-origin",
        "--max-request-size",
        "--max-image-pixels",
        "--request-timeout",
        "--queue-size",
        "--idle-release",
        "--disable-ane",
        "--allow-idle-sleep",
        "--help",
        "--version",
    }
)


def managed_flag_for(flag: str) -> str | None:
    """The managed flag `flag` names, exactly or as an argparse abbreviation.

    Splash's parsers keep argparse's default `allow_abbrev=True`
    (`install/launcher.py` `parse_args`), so `--hos 0.0.0.0` *is* `--host 0.0.0.0`
    to the engine and an exact-match check alone would let it through.
    """
    if flag in MANAGED_FLAGS:
        return flag
    matches = sorted(m for m in MANAGED_FLAGS if m.startswith(flag))
    return matches[0] if matches else None


@dataclass(frozen=True)
class Issue:
    path: tuple[str | int, ...]
    message: str
    severity: Severity = "error"
    code: str = "invalid"

    @property
    def model(self) -> str | None:
        return str(self.path[1]) if len(self.path) > 1 and self.path[0] == "models" else None

    @property
    def key(self) -> str:
        """The metadata key: `serve.max_context`, `server.port`."""
        rest = self.path[1:] if self.path[:1] == ("global",) else self.path[2:]
        return ".".join(str(part) for part in rest)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": list(self.path),
            "key": self.key,
            "model": self.model,
            "message": self.message,
            "severity": self.severity,
            "code": self.code,
        }


@dataclass(frozen=True)
class ValidationContext:
    api_key_present: bool = True
    installed_models: frozenset[str] | None = None


@dataclass
class ValidationResult:
    document: SettingsDocument | None
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.document is not None and not self.errors


def _clean_message(message: str) -> str:
    for prefix in ("Value error, ", "Assertion failed, "):
        if message.startswith(prefix):
            return message[len(prefix) :]
    return message


def pydantic_issues(error: ValidationError) -> list[Issue]:
    issues = []
    for item in error.errors():
        loc = tuple(part for part in item["loc"] if part not in ("function-after",))
        issues.append(Issue(path=loc, message=_clean_message(item["msg"]), code=item["type"]))
    return issues


def validate_document(raw: Any, context: ValidationContext | None = None) -> ValidationResult:
    """Validate a settings document as received from the UI or read from disk."""
    context = context or ValidationContext()
    try:
        doc = SettingsDocument.model_validate(raw)
    except ValidationError as error:
        return ValidationResult(document=None, errors=pydantic_issues(error))
    issues = list(cross_field_issues(doc, context))
    return ValidationResult(
        document=doc,
        errors=[i for i in issues if i.severity == "error"],
        warnings=[i for i in issues if i.severity == "warning"],
    )


def _is_yarn(max_context: Any) -> bool:
    """A context limit past the native window, which turns on YaRN."""
    try:
        tokens = p.parse_max_context(str(max_context))
    except ValueError:
        return False
    return tokens is not None and tokens > p.MAX_CONTEXT_TOKENS


def cross_field_issues(doc: SettingsDocument, context: ValidationContext) -> Iterable[Issue]:
    g = doc.global_
    serve = g.serve

    cache_bytes = p.parse_max_cache_disk(serve.max_cache_disk)
    if serve.persistent_cache and cache_bytes == 0:
        yield Issue(
            ("global", "serve", "persistent_cache"),
            PERSISTENT_NEEDS_CACHE,
            code="requires",
        )
    if cache_bytes == 0 and p.parse_max_cache_disk_writes(serve.max_cache_disk_writes):
        yield Issue(
            ("global", "serve", "max_cache_disk_writes"),
            WRITES_NEED_CACHE,
            code="requires",
        )
    if cache_bytes is not None and 0 < cache_bytes < max(MIN_USEFUL_CACHE_DISK.values()):
        yield Issue(
            ("global", "serve", "max_cache_disk"),
            "Below about one state's size (64 MiB for 35B, 153 MiB for 27B) Splash "
            "leaves the SSD cache off",
            severity="warning",
            code="too_small",
        )
    if serve.kv_format == "bf16":
        yield Issue(
            ("global", "serve", "kv_format"),
            "≈2× KV memory, can be slower at long context",
            severity="warning",
            code="tradeoff",
        )
    if serve.state_format == "bf16":
        yield Issue(
            ("global", "serve", "state_format"),
            STATE_BF16_TRADEOFF,
            severity="warning",
            code="tradeoff",
        )

    if _is_yarn(serve.max_context):
        yield Issue(
            ("global", "serve", "max_context"), YARN_TRADEOFF, severity="warning", code="yarn"
        )
    if cache_bytes == 0 and p.parse_sparse_kv_memory(serve.sparse_kv_memory) is not None:
        yield Issue(
            ("global", "serve", "sparse_kv_memory"),
            SPARSE_KV_NEEDS_CACHE,
            severity="warning",
            code="ignored",
        )

    if not p.is_loopback_host(g.server.host):
        if not g.security.api_key_required:
            yield Issue(
                ("global", "security", "api_key_required"),
                "A local-network bind requires an API key",
                code="lan_requires_key",
            )
        elif not context.api_key_present:
            yield Issue(
                ("global", "security", "api_key_required"),
                "Generate an API key before binding to the local network",
                code="api_key_missing",
            )
        if not g.security.admin_requires_key:
            # A LAN bind forces admin sign-in (refused, never switched on silently).
            yield Issue(
                ("global", "security", "admin_requires_key"),
                "A local-network bind requires admin sign-in",
                code="lan_requires_admin_key",
            )
    elif g.security.api_key_required and not context.api_key_present:
        yield Issue(
            ("global", "security", "api_key_required"),
            "Generate an API key before requiring one",
            code="api_key_missing",
        )
    if g.security.admin_requires_key and not context.api_key_present:
        yield Issue(
            ("global", "security", "admin_requires_key"),
            "Generate an API key before protecting the admin",
            code="api_key_missing",
        )
    if "*" in g.server.allowed_origins and not g.security.api_key_required:
        yield Issue(
            ("global", "server", "allowed_origins"),
            "'*' without an API key lets any web page call the API",
            severity="warning",
            code="open_origin",
        )

    ports = {"server.port": g.server.port}
    if isinstance(g.engine.internal_port, int):
        if g.engine.internal_port == g.server.port:
            yield Issue(
                ("global", "engine", "internal_port"),
                "must differ from the public port",
                code="port_conflict",
            )
        ports["engine.internal_port"] = g.engine.internal_port
    if g.integrations.claude_desktop.port in ports.values():
        yield Issue(
            ("global", "integrations", "claude_desktop", "port"),
            "must differ from the public and engine ports",
            code="port_conflict",
        )

    for index, extra in enumerate(g.engine.extra_flags):
        managed = managed_flag_for(extra.flag)
        if managed is not None:
            yield Issue(
                ("global", "engine", "extra_flags", index, "flag"),
                f"{extra.flag} is set through its own setting"
                if managed == extra.flag
                else f"{extra.flag} abbreviates {managed}, which is set through its own setting",
                code="managed_flag",
            )

    if (
        g.routing.default_model is not None
        and context.installed_models is not None
        and g.routing.default_model not in context.installed_models
    ):
        yield Issue(
            ("global", "routing", "default_model"),
            f"{g.routing.default_model} is not installed",
            severity="warning",
            code="not_installed",
        )
    if g.advanced.crash_trace:
        yield Issue(
            ("global", "advanced", "crash_trace"),
            "Crash traces can contain private conversation data",
            severity="warning",
            code="privacy",
        )

    for model_id in doc.models:
        yield from _model_issues(doc, model_id)


def _model_issues(doc: SettingsDocument, model_id: str) -> Iterable[Issue]:
    values = effective_values(doc, model_id)
    base = ("models", model_id, "serve")
    if values["serve.announce_served_name"].value and not values["serve.served_model_names"].value:
        yield Issue(
            (*base, "announce_served_name"),
            "--announce-served-name needs --served-model-name",
            code="requires",
        )
    if refusal := p.package_refusal(model_id):
        yield Issue(("models", model_id), refusal, severity="warning", code="package")
    if values["serve.kv_format"].source == "model" and values["serve.kv_format"].value == "bf16":
        yield Issue(
            (*base, "kv_format"),
            "≈2× KV memory, can be slower at long context",
            severity="warning",
            code="tradeoff",
        )
    state = values["serve.state_format"]
    if state.source == "model" and state.value == "bf16":
        yield Issue(
            (*base, "state_format"), STATE_BF16_TRADEOFF, severity="warning", code="tradeoff"
        )
    context = values["serve.max_context"]
    if context.source == "model" and _is_yarn(context.value):
        yield Issue((*base, "max_context"), YARN_TRADEOFF, severity="warning", code="yarn")
    sparse = values["serve.sparse_kv_memory"]
    if (
        sparse.source == "model"
        and p.parse_max_cache_disk(doc.global_.serve.max_cache_disk) == 0
        and p.parse_sparse_kv_memory(str(sparse.value)) is not None
    ):
        yield Issue(
            (*base, "sparse_kv_memory"), SPARSE_KV_NEEDS_CACHE, severity="warning", code="ignored"
        )

"""Admin API request/response models.

Conventions: snake_case fields; record timestamps are ISO 8601 UTC strings;
metric sample times (`t`) are Unix seconds; sizes are bytes; durations are
milliseconds (`*_ms`) or seconds (`*_s`). Every error is `ErrorResponse`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .settings.metadata import Applies, Control, Scope
from .settings.model import McpServer, SamplingOverlay, SettingsDocument


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


# Common ---------------------------------------------------------------------


class IssueOut(ApiModel):
    path: list[str | int]
    key: str
    model: str | None = None
    message: str
    severity: Literal["error", "warning"] = "error"
    code: str = "invalid"


class ErrorBody(ApiModel):
    message: str
    type: str
    code: str
    issues: list[IssueOut] | None = None
    details: dict[str, Any] | None = None


class ErrorResponse(ApiModel):
    error: ErrorBody


class OkResponse(ApiModel):
    ok: bool = True


class JobAccepted(ApiModel):
    job_id: str
    kind: str


class DeleteCount(ApiModel):
    deleted: int


# Engine ---------------------------------------------------------------------

EngineStateName = Literal[
    "stopped",
    "starting",
    "ready",
    "busy",
    "idle_released",
    "recovering",
    "engine_failed",
    "stopping",
    "crashed",
    "failed",
]
EnginePhase = Literal["installing", "loading", "warming"]
EngineErrorKind = Literal[
    "budget_refusal",
    "gated",
    "auth",
    "unknown_model",
    "incompatible",
    "port_in_use",
    "engine_missing",
    "crash_loop",
    "startup_timeout",
    "other",
]
SuggestionAction = Literal[
    "lower_max_context",
    "language_only",
    "raise_max_memory",
    "smaller_variant",
    "enable_ssd_cache",
    "add_hf_token",
    "retry",
    "go_offline",
    "open_logs",
    "restart_engine",
]


class EngineDiscoveryInfo(ApiModel):
    found: bool
    cli: str | None = None
    source: Literal["setting", "env", "brew", "path"] | None = None
    version: str | None = None
    support: Literal["supported", "untested", "too_old", "unknown"] = "unknown"
    supported_range: str = ">=1.3.0 <1.4.0"
    banner: str | None = None
    source_checkout: bool = False
    pkg: str | None = None
    python: str | None = None
    error: str | None = None


class MemoryBudgetRow(ApiModel):
    label: str
    bytes: int | None = None
    text: str


class EngineSuggestion(ApiModel):
    action: SuggestionAction
    label: str
    # Settings the action would apply, e.g. {"serve.max_context": "64K"}.
    patch: dict[str, Any] | None = None


class EngineError(ApiModel):
    kind: EngineErrorKind
    code: str
    message: str
    raw: list[str] = Field(default_factory=list)
    budget: list[MemoryBudgetRow] | None = None
    suggestions: list[EngineSuggestion] = Field(default_factory=list)


class EngineNotice(ApiModel):
    kind: Literal[
        "disk_tier_suggestion",
        "hub_unreachable",
        "new_commit_not_installed",
        "weights_restored",
        "schema_unrecognized",
        "other",
    ]
    message: str
    raw: str | None = None
    ts: str


class RestartInfo(ApiModel):
    auto_restart: bool
    attempt: int = 0
    crashes_in_window: int = 0
    next_retry_at: str | None = None
    backoff_s: float | None = None


class TransportInfo(ApiModel):
    recovering: bool = False
    stopped: bool = False
    error: str | None = None


class EngineInstall(ApiModel):
    """What `splash serve` is downloading before it loads (`starting.installing`), from
    Splash's `Fetching N file(s), X GB, from REPO@REV` line and the Hub cache.
    Stopping the engine now loses the file in progress (huggingface_hub 1.28, Q24)."""

    repo: str
    revision: str
    files: int
    total_bytes: int
    done_bytes: int = 0
    speed_bps: float | None = None
    eta_s: float | None = None


class EngineView(ApiModel):
    """The engine state machine plus a summary for headers and the menu bar."""

    state: EngineStateName
    phase: EnginePhase | None = None
    model: str | None = None
    since: str
    started_at: str | None = None
    ready_at: str | None = None
    uptime_s: float | None = None
    pid: int | None = None
    internal_port: int | None = None
    requests_in_flight: int = 0
    queued: int = 0
    maximum_context_tokens: int | None = None
    chat_template_mode: Literal["native", "patched", "unsupported"] | None = None
    kv_format: str | None = None
    vision: bool | None = None
    draft: str | None = None
    status_schema_version: int | None = None
    schema_supported: bool = True
    transport: TransportInfo | None = None
    restart: RestartInfo
    error: EngineError | None = None
    notices: list[EngineNotice] = Field(default_factory=list)
    log_tail: list[str] = Field(default_factory=list)
    command: str | None = None
    # What the persistent cache restored at this start (`/status.disk.taken_back`,
    # or Splash's startup line), for "Restored at start: N states …".
    # Keys: states, kv_blocks, bytes, left_behind. Null when not persistent/unknown.
    taken_back: dict[str, int] | None = None
    persistent_cache: bool | None = None
    # Set while `starting.installing` downloads files (null otherwise).
    install: EngineInstall | None = None
    engine: EngineDiscoveryInfo


class LoadRequest(ApiModel):
    model: str
    # Switch even with requests in flight (they are cut off), or while Splash is
    # downloading the current model's files (the file in progress is lost).
    force: bool = False
    # Answer only once the model is ready or failed (CLI `load`, G25).
    wait: bool = False
    # Seconds to wait with `wait`; default routing.load_timeout.
    timeout: float | None = Field(default=None, gt=0, le=3600)


class RestartRequest(ApiModel):
    # Restart even with requests in flight, or while Splash is downloading the
    # model's files (the file in progress starts again from byte 0, Q24). The
    # `?force=true` query parameter is still accepted.
    force: bool = False


# System ---------------------------------------------------------------------


class VolumeInfo(ApiModel):
    path: str
    total_bytes: int
    free_bytes: int


class PowerInfo(ApiModel):
    source: Literal["ac", "battery", "unknown"] = "unknown"
    battery_percent: int | None = None
    low_power_mode: bool | None = None


class SystemDisks(ApiModel):
    models: VolumeInfo
    cache: VolumeInfo


class SystemInfo(ApiModel):
    chip: str | None = None
    gpu_cores: int | None = None
    cpu_cores: int | None = None
    memory_bytes: int
    macos_version: str
    macos_build: str | None = None
    arch: str
    hostname: str
    supported: bool
    unsupported_reasons: list[str] = Field(default_factory=list)
    disk: SystemDisks
    power: PowerInfo


class UpdateInfo(ApiModel):
    available: bool = False
    version: str | None = None
    release_notes_md: str | None = None
    url: str | None = None
    checked_at: str | None = None


class Versions(ApiModel):
    gui: str
    manager: str
    python: str
    engine: EngineDiscoveryInfo
    status_schema_version: int | None = None
    engine_update: UpdateInfo


class DoctorCheck(ApiModel):
    id: str
    label: str
    status: Literal["ok", "warn", "fail", "skip"]
    message: str
    fix: str | None = None


class DoctorReport(ApiModel):
    ok: bool
    checks: list[DoctorCheck]


class BrewInfo(ApiModel):
    installed: bool
    path: str | None = None
    version: str | None = None
    splash_formula_installed: bool = False


class RevealRequest(ApiModel):
    target: Literal["model", "models_dir", "cache_dir", "logs_dir", "trace", "splash_data_dir"]
    id: str | None = None


# Events, alerts, notifications -------------------------------------------------

AlertCondition = Literal[
    "engine_recovering",
    "engine_failed",
    "crash_loop",
    "memory_critical",
    "memory_warning",
    "capacity_exhausted",
    "resource_timeout",
    "disk_tier_failures",
    "write_behind_refused",
    "queue_full",
    "mask_timeout",
    "download_failed",
    "download_done",
    "update_available",
    "schema_unrecognized",
    "unclean_integration_shutdown",
]


class NotificationAction(ApiModel):
    id: str
    label: str
    method: Literal["GET", "POST", "PUT", "DELETE"] = "POST"
    path: str
    body: dict[str, Any] | None = None


class Alert(ApiModel):
    id: str
    condition: AlertCondition
    severity: Literal["info", "warn", "critical"]
    title: str
    message: str
    source: Literal["status", "proxy", "supervisor", "downloader", "updater", "integrations"]
    raised_at: str
    updated_at: str
    count: int = 1
    dismissible: bool = True
    actions: list[NotificationAction] = Field(default_factory=list)


class AlertList(ApiModel):
    alerts: list[Alert]


class Notification(ApiModel):
    id: str
    kind: AlertCondition | Literal["info"]
    title: str
    body: str
    ts: str
    actions: list[NotificationAction] = Field(default_factory=list)


class SettingChange(ApiModel):
    key: str
    model: str | None = None
    applies: Applies


class SettingsChangedEvent(ApiModel):
    changed: list[SettingChange]
    restart_required: bool


class ModelsChangedEvent(ApiModel):
    reason: Literal["downloaded", "deleted", "verified", "updated", "imported", "moved", "other"]
    model: str | None = None


class EngineUpgradeEvent(ApiModel):
    phase: Literal[
        "stopping", "updating", "upgrading", "rediscovering", "restarting", "done", "failed"
    ]
    line: str | None = None
    ok: bool | None = None


class JobEvent(ApiModel):
    """Progress of a background job (verify, storage move, import, engine install)."""

    job_id: str
    kind: Literal["verify", "storage_move", "import", "engine_install", "engine_upgrade"]
    state: Literal["running", "done", "failed"]
    model: str | None = None
    progress: float | None = None
    message: str | None = None
    line: str | None = None


class JobView(ApiModel):
    """A background job's current state and its last output lines (`GET /jobs/{id}`)."""

    job_id: str
    kind: Literal["verify", "storage_move", "import", "engine_install", "engine_upgrade"]
    state: Literal["running", "done", "failed"]
    model: str | None = None
    progress: float | None = None
    message: str | None = None
    lines: list[str] = Field(default_factory=list)


class TokenPiece(ApiModel):
    id: int
    piece: str | None  # the vocabulary entry (byte-level form, e.g. "Ġhello")
    text: str  # what the id decodes to on its own


class TokenPieces(ApiModel):
    model: str
    pieces: list[TokenPiece]


class TokenPiecesRequest(ApiModel):
    ids: list[int] = Field(max_length=32768)
    model: str | None = None  # default: the active model


class JobList(ApiModel):
    jobs: list[JobView]


class AlertCleared(ApiModel):
    id: str


class LogBackfill(ApiModel):
    lines: list[LogLine]


class ProcessExit(ApiModel):
    """Last event of a streamed subprocess (trace replay)."""

    code: int


class BenchmarkProgressEvent(ApiModel):
    run_id: str
    scenario: str
    step: int
    total_steps: int
    state: Literal["warmup", "running", "done", "cancelled", "failed"]
    message: str | None = None


# Metrics ---------------------------------------------------------------------


class ThroughputMetrics(ApiModel):
    decode_tps: float | None = None
    decode_tps_cycle: float | None = None
    prefill_tps: float | None = None
    prefill_tps_delta: float | None = None


class TotalsMetrics(ApiModel):
    prompt_tokens: int | None = None
    decode_tokens: int | None = None
    total_tokens: int | None = None
    reused_tokens: int | None = None
    requests_submitted: int | None = None
    requests_completed: int | None = None
    requests_failed: int | None = None
    requests_cancelled: int | None = None


class CacheMetrics(ApiModel):
    efficiency: float | None = None
    hit_rate: float | None = None
    hits: int | None = None
    cold_misses: int | None = None


class DraftMetrics(ApiModel):
    acceptance_rate: float | None = None
    drafted_tokens: int | None = None
    accepted_tokens: int | None = None


class LatencyMetrics(ApiModel):
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    itl_p50_ms: float | None = None
    itl_p95_ms: float | None = None


class SchedulerMetrics(ApiModel):
    decoding: int | None = None
    prefilling: int | None = None
    queued: int | None = None
    waiting_resources: int | None = None
    waiting_prefix: int | None = None
    waiting_mask: int | None = None


class MemoryMetrics(ApiModel):
    current_bytes: int | None = None
    peak_bytes: int | None = None
    allocated_bytes: int | None = None
    limit_bytes: int | None = None
    charged_bytes: int | None = None
    headroom_bytes: int | None = None
    host_available_bytes: int | None = None
    host_reserve_bytes: int | None = None
    system_pressure: Literal["normal", "warning", "critical"] | None = None
    growth_allowed: bool | None = None


class KvMetrics(ApiModel):
    pages_active: int | None = None
    pages_cache: int | None = None
    pages_free: int | None = None
    pages_allocated: int | None = None


class DiskMetrics(ApiModel):
    enabled: bool = False
    persistent: bool = False
    read_bps: float | None = None
    written_bps: float | None = None
    used_bytes: int | None = None
    capacity_bytes: int | None = None


class StageLatency(ApiModel):
    p50_ms: float | None = None
    p95_ms: float | None = None
    count: int = 0


class LiveMetrics(ApiModel):
    """One derived sample, from two consecutive /status polls."""

    t: float
    engine_state: EngineStateName
    model: str | None = None
    restarted: bool = False
    throughput: ThroughputMetrics = Field(default_factory=ThroughputMetrics)
    totals: TotalsMetrics = Field(default_factory=TotalsMetrics)
    cache: CacheMetrics = Field(default_factory=CacheMetrics)
    draft: DraftMetrics = Field(default_factory=DraftMetrics)
    latency: LatencyMetrics = Field(default_factory=LatencyMetrics)
    scheduler: SchedulerMetrics = Field(default_factory=SchedulerMetrics)
    memory: MemoryMetrics = Field(default_factory=MemoryMetrics)
    kv: KvMetrics = Field(default_factory=KvMetrics)
    disk: DiskMetrics = Field(default_factory=DiskMetrics)
    # /status.latency histograms -> percentiles, by stage name (http_ttft, preparation, ...).
    stages: dict[str, StageLatency] = Field(default_factory=dict)


class MetricsSeries(ApiModel):
    """Columnar ring-buffer history for uPlot: `series[key][i]` belongs to `t[i]`."""

    window_s: int
    step_s: int = 1
    t: list[float]
    series: dict[str, list[float | None]]


# Settings ---------------------------------------------------------------------


class SecretsState(ApiModel):
    api_key_set: bool
    hf_token_override_set: bool
    hf_login_token_present: bool


class ResolvedPaths(ApiModel):
    base: str
    models_dir: str
    cache_dir: str
    tmp_dir: str
    splash_data_dir: str
    crash_trace_dir: str


class SettingsResponse(ApiModel):
    settings: SettingsDocument
    secrets: SecretsState
    resolved: ResolvedPaths
    read_only: bool = False
    load_warnings: list[str] = Field(default_factory=list)


class SettingsSaveResult(ApiModel):
    settings: SettingsDocument
    restart_required: bool
    changed: list[SettingChange]
    warnings: list[IssueOut] = Field(default_factory=list)


class SettingsValidation(ApiModel):
    valid: bool
    errors: list[IssueOut]
    warnings: list[IssueOut]


class EngineOptionOut(ApiModel):
    flag: str
    dest: str
    default: Any = None
    help: str
    choices: list[Any] | None = None
    metavar: str | None = None
    action: str
    environment: str | None = None
    secret: bool = False
    source: str
    takes_value: bool
    known: bool


class SchemaField(ApiModel):
    key: str
    label: str
    help: str
    section: str
    control: Control
    applies: Applies
    scope: Scope
    flag: str | None = None
    env: str | None = None
    advanced: bool = False
    choices: list[str] | None = None
    min: float | None = None
    max: float | None = None
    unit: str | None = None
    storage: Literal["settings", "keychain"] = "settings"
    default: Any = None
    warnings: list[str] = Field(default_factory=list)
    disabled_for_legacy: bool = False
    engine: EngineOptionOut | None = None


class SchemaSection(ApiModel):
    id: str
    label: str
    fields: list[str]


class EngineOptionsInfo(ApiModel):
    available: bool
    version: str | None = None
    errors: list[str] = Field(default_factory=list)
    unknown: list[EngineOptionOut] = Field(default_factory=list)


class SettingsSchema(ApiModel):
    sections: list[SchemaSection]
    fields: list[SchemaField]
    profile_fields: list[SchemaField]
    engine_options: EngineOptionsInfo


class EffectiveValueOut(ApiModel):
    key: str
    value: Any = None
    source: Literal["default", "global", "model"]
    default: Any = None
    global_value: Any = None
    model_value: Any = None


class ProfileOut(ApiModel):
    name: str
    id: str
    builtin: bool
    modified: bool
    overlay: dict[str, Any]


class EffectiveSettings(ApiModel):
    model: str | None
    legacy: bool
    values: dict[str, EffectiveValueOut]
    profiles: list[ProfileOut] = Field(default_factory=list)
    sampling_defaults: dict[str, Any] = Field(default_factory=dict)


class LaunchPreview(ApiModel):
    model: str
    argv: list[str]
    env: dict[str, str]
    display: str
    error: str | None = None


class ModelPickOut(ApiModel):
    model: str
    note: str
    overrides: dict[str, Any] = Field(default_factory=dict)


class RecommendationOut(ApiModel):
    primary: ModelPickOut | None
    alternatives: list[ModelPickOut]
    reason: str


class PresetOut(ApiModel):
    id: Literal["coding", "chat", "speed"]
    label: str
    description: str
    settings: dict[str, Any]
    recommendation: RecommendationOut


class PresetList(ApiModel):
    memory_bytes: int
    presets: list[PresetOut]


class PresetApplyRequest(ApiModel):
    model: str | None = None


class ApiKeyOut(ApiModel):
    key: str | None


class HfTokenIn(ApiModel):
    token: str


class HfTokenTestIn(ApiModel):
    token: str | None = None


class HfWhoami(ApiModel):
    """`GET /hf/whoami`: which token the manager uses and who it belongs to."""

    status: Literal["ok", "no_token", "rejected", "unreachable"]
    source: Literal["override", "env", "hf_login", "none"]
    user: str | None = None
    orgs: list[str] = Field(default_factory=list)
    http_status: int | None = None
    message: str | None = None


class SecretMeta(ApiModel):
    """A secret's presence and mask; the value itself never leaves the Keychain."""

    name: Literal["api_key", "hf_token"]
    set: bool
    prefix: str | None = None
    last4: str | None = None
    masked: str | None = None  # "{prefix}••••{last4}", or "••••" for short values
    updated_at: str | None = None


class SettingsResetRequest(ApiModel):
    # Restart the engine afterwards when the active model's flags changed.
    restart_engine: bool = True
    # Restart even with requests in flight (otherwise 409 model_switch_busy).
    force: bool = False


class SettingsResetResult(ApiModel):
    settings: SettingsDocument
    restart_required: bool
    engine_restarted: bool = False
    kept: list[str] = Field(default_factory=list)


class HfTokenTestOut(ApiModel):
    ok: bool
    source: Literal["provided", "override", "env", "hf_login", "none"]
    user: str | None = None
    orgs: list[str] = Field(default_factory=list)
    error: str | None = None


# Profiles ---------------------------------------------------------------------


class ProfilesView(ApiModel):
    model: str
    profiles: list[ProfileOut]
    sampling_defaults: dict[str, Any]


class ProfilesUpdate(ApiModel):
    """Replaces the model's stored profiles and/or sampling defaults (absent = unchanged).
    A built-in profile's overlay may be edited; `null` hides it (not `default`)."""

    profiles: dict[str, SamplingOverlay | None] | None = None
    sampling_defaults: SamplingOverlay | None = None


# Models -------------------------------------------------------------------------

ModelFormat = Literal["mlx", "gguf", "legacy"]
Family = Literal["Qwen3.8-27B", "Qwen3.6-35B-A3B"]
Fit = Literal["fits", "tight", "wont_fit"]


class DraftRef(ApiModel):
    repo_id: str
    commit: str | None = None
    shared: bool = False


class InstalledModel(ApiModel):
    id: str
    repo_id: str
    variant: str | None = None
    family: Family | None = None
    format: ModelFormat
    language_only: bool = False
    vision: bool | None = None
    size_bytes: int
    unique_bytes: int
    revision: str | None = None
    commit: str | None = None
    pinned: bool = False
    legacy: bool = False
    last_used_at: str | None = None
    status: Literal[
        "active",
        "loading",
        "ready",
        "downloading",
        "paused",
        "verifying",
        "update_available",
        "broken",
    ]
    progress: float | None = None
    draft: DraftRef | None = None


class DiskUsage(ApiModel):
    models_dir: str
    cache_dir: str
    models_bytes: int
    cache_bytes: int
    free_bytes: int
    total_bytes: int


class InstalledModels(ApiModel):
    models: list[InstalledModel]
    disk: DiskUsage


class ModelFile(ApiModel):
    path: str
    repo_id: str
    size_bytes: int | None = None
    role: Literal["weights", "config", "tokenizer", "mmproj", "draft", "other"] = "other"


class ModelFingerprints(ApiModel):
    """What the engine reported at this model's last load (`/status.identity`,
    stored in usage.db `model_facts`; Info)."""

    build_id: str | None = None
    loaded_model_layout_sha256: str | None = None
    target_model_sha256: str | None = None
    kv_format: str | None = None
    kv_quantization: str | None = None
    # The whole `identity` object as Splash wrote it (tolerant of new keys).
    identity: dict[str, Any] | None = None
    max_context: int | None = None
    vision: bool | None = None
    recorded_at: str | None = None


class ModelDetail(InstalledModel):
    files: list[ModelFile] = Field(default_factory=list)
    latest_commit: str | None = None
    update_available: bool = False
    chat_template_mode: Literal["native", "patched", "unsupported"] | None = None
    link_path: str | None = None
    # Null until the model has been loaded once.
    fingerprints: ModelFingerprints | None = None


class DeleteModelResult(ApiModel):
    deleted: list[str]
    freed_bytes: int
    kept_draft: bool
    engine_stopped: bool


class VerifyRequest(ApiModel):
    full: bool = False


class VariantOut(ApiModel):
    name: str
    size_bytes: int | None = None
    bits_per_weight: float | None = None
    quality: str | None = None
    loadable: bool | None = None
    reason: str | None = None
    fit: Fit | None = None
    recommended: bool = False
    files: list[str] = Field(default_factory=list)
    # Catalog only (null in /inspect, which has download_plan): what downloading this
    # variant fetches in total, target + projector + draft, and without the projector.
    download_bytes: int | None = None
    language_only_download_bytes: int | None = None


class VisionInfo(ApiModel):
    available: bool
    reason: str | None = None
    projector: str | None = None


class PlannedFile(ApiModel):
    name: str
    repo_id: str
    bytes: int | None = None
    # Already in the models directory (shared draft, another variant's files).
    present: bool = False


class DownloadPlan(ApiModel):
    """The files `prepare` will fetch for this ID."""

    variant: str | None = None
    language_only: bool = False
    files: list[PlannedFile] = Field(default_factory=list)
    total_bytes: int = 0
    remaining_bytes: int = 0
    free_bytes: int | None = None
    margin_bytes: int = 2 * 1024**3
    # remaining_bytes + margin_bytes <= free_bytes (the §9.4 disk check).
    fits_on_disk: bool = True


class InspectResult(ApiModel):
    """Compatibility output, cached by repo@sha for 24 h."""

    id: str
    repo_id: str
    commit: str | None = None
    compatible: bool
    badge: Literal["compatible", "text_only", "incompatible", "not_clef_accurate"]
    family: Family | None = None
    format: ModelFormat | None = None
    variants: list[VariantOut] = Field(default_factory=list)
    recommended_variant: str | None = None
    vision: VisionInfo
    draft: str | None = None
    # A plain line; when Splash's own refusal is technical (an MLX checkpoint that
    # is not 4-bit group 64), `reason_detail` keeps the engine's exact words (D53).
    reason: str | None = None
    reason_detail: str | None = None
    memory_need_bytes: int | None = None
    fit: Fit | None = None
    cached: bool = False
    checked_at: str
    # Recomputed on every call (presence and free space change); null when
    # incompatible or the file set is unknown.
    download_plan: DownloadPlan | None = None
    language_only_plan: DownloadPlan | None = None


class CatalogEntry(ApiModel):
    id: str
    repo_id: str
    family: Family
    format: ModelFormat
    notes: str | None = None
    # The target weights (GGUF: the default variant plus its projector).
    size_bytes: int | None = None
    # What downloading the default selection fetches in total (the recommended variant
    # for GGUF; target + vision + draft), from the same file plan as /inspect's
    # download_plan.total_bytes; and the same without vision (--language-only).
    # Null offline or when the draft's listing is unavailable.
    download_bytes: int | None = None
    language_only_download_bytes: int | None = None
    memory_need_bytes: int | None = None
    fit: Fit | None = None
    vision: bool | None = None
    license: str | None = None
    installed: bool = False
    recommended: bool = False
    variants: list[VariantOut] | None = None
    # GGUF: the variant to download by default (the §8.6 pick for this Mac when
    # this repository is it, else the largest ≤ Q4-class variant that fits).
    recommended_variant: str | None = None
    last_modified: str | None = None
    perf_note: str | None = None


class CatalogGroup(ApiModel):
    format: str
    label: str
    entries: list[CatalogEntry]


class CatalogFamily(ApiModel):
    family: Family
    label: str
    groups: list[CatalogGroup]


class Catalog(ApiModel):
    families: list[CatalogFamily]
    memory_bytes: int
    refreshed_at: str | None = None
    offline: bool = False


class SearchResult(ApiModel):
    id: str
    downloads: int | None = None
    likes: int | None = None
    last_modified: str | None = None
    tags: list[str] = Field(default_factory=list)
    format_guess: Literal["mlx", "gguf", "unknown"] = "unknown"


class SearchResults(ApiModel):
    query: str
    sort: Literal["downloads", "likes", "recent"]
    results: list[SearchResult]


class ModelCard(ApiModel):
    id: str
    markdown: str
    license: str | None = None
    tags: list[str] = Field(default_factory=list)
    gated: bool = False
    files: list[ModelFile] = Field(default_factory=list)


# Downloads ------------------------------------------------------------------------

DownloadState = Literal["queued", "running", "paused", "verifying", "done", "failed", "cancelled"]


class DownloadFile(ApiModel):
    name: str
    repo_id: str
    size_bytes: int | None = None
    done_bytes: int = 0
    state: Literal["pending", "downloading", "done"] = "pending"


class DownloadError(ApiModel):
    code: Literal[
        "gated",
        "disk_full",
        "hub_unreachable",
        "incompatible",
        "verify_failed",
        "installer_failed",
        "cancelled",
    ]
    message: str
    action: Literal["add_hf_token", "free_space", "retry", "go_offline"] | None = None
    needed_bytes: int | None = None
    free_bytes: int | None = None


class DownloadItem(ApiModel):
    id: str
    model: str
    revision: str | None = None
    draft_model: str | None = None
    language_only: bool = False
    verify: bool = True
    state: DownloadState
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    bytes_total: int | None = None
    bytes_done: int = 0
    progress: float | None = None
    speed_bps: float | None = None
    eta_s: float | None = None
    files: list[DownloadFile] = Field(default_factory=list)
    error: DownloadError | None = None
    log_tail: list[str] = Field(default_factory=list)


class DownloadList(ApiModel):
    items: list[DownloadItem]
    parallel: int


class DownloadRequest(ApiModel):
    id: str
    revision: str | None = None
    draft_model: str | None = None
    language_only: bool = False
    verify: bool = True


# Chats ------------------------------------------------------------------


class ChatAttachment(ApiModel):
    kind: Literal["image", "pdf"]
    file: str
    name: str | None = None
    bytes: int | None = None


class ChatMessageMeta(ApiModel):
    model_config = ConfigDict(extra="allow")

    usage: dict[str, Any] | None = None
    timings: dict[str, Any] | None = None
    ttft_ms: float | None = None
    finish_reason: str | None = None
    profile: str | None = None


class ChatMessage(ApiModel):
    id: str
    parent: str | None = None
    role: Literal["user", "assistant", "tool", "system"]
    content: str | list[dict[str, Any]]
    reasoning: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    meta: ChatMessageMeta | None = None
    attachments: list[ChatAttachment] = Field(default_factory=list)
    created_at: str | None = None


class Chat(ApiModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    model: str | None = None
    profile: str = "default"
    system: str | None = None
    sampling: dict[str, Any] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: Any = None
    response_format: dict[str, Any] | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    active_leaf: str | None = None


class ChatCreate(ApiModel):
    title: str | None = None
    model: str | None = None
    profile: str | None = None
    system: str | None = None
    sampling: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    response_format: dict[str, Any] | None = None


class ChatSummary(ApiModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    model: str | None = None
    profile: str = "default"
    message_count: int
    snippet: str | None = None


class ChatList(ApiModel):
    chats: list[ChatSummary]


class AttachmentUpload(ApiModel):
    file: str
    sha256: str
    bytes: int
    kind: Literal["image", "pdf"]


# MCP ------------------------------------------------------------------------------


class McpServers(ApiModel):
    servers: dict[str, McpServer]


class McpTool(ApiModel):
    server: str
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    always_allow: bool = False


class McpServerError(ApiModel):
    server: str
    message: str


class McpToolList(ApiModel):
    tools: list[McpTool]
    errors: list[McpServerError] = Field(default_factory=list)


class McpCallRequest(ApiModel):
    server: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False


class McpCallResult(ApiModel):
    content: list[dict[str, Any]]
    structured_content: Any = None
    is_error: bool = False
    # Wall time of the call (UI gap G6).
    duration_ms: float | None = None


# Usage ---------------------------------------------------------------


class UsageRow(ApiModel):
    id: int
    ts: str
    model: str | None
    profile: str | None = None
    endpoint: str
    client: str | None = None
    stream: bool
    status: int
    error_code: str | None = None
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    completion_tokens: int | None = None
    ttft_ms: float | None = None
    prompt_ms: float | None = None
    predicted_ms: float | None = None
    duration_ms: float | None = None
    priority: str | None = None
    injected: dict[str, Any] | None = None


class UsageRows(ApiModel):
    rows: list[UsageRow]
    next_cursor: str | None = None
    # Rows matching the filters, and where this page starts (0-based), for the
    # "1–50 of 1,284" range. `offset` is null for a cursor page.
    total: int = 0
    offset: int | None = None
    limit: int = 100


class UsageFacets(ApiModel):
    """Distinct values among the rows in range: the history filters' options."""

    models: list[str] = Field(default_factory=list)
    endpoints: list[str] = Field(default_factory=list)
    clients: list[str] = Field(default_factory=list)
    profiles: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=lambda: ["2xx", "4xx", "5xx", "cancelled"])


class UsageModelSummary(ApiModel):
    model: str
    requests: int
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    last_used_at: str | None = None


class UsageClientSummary(ApiModel):
    client: str
    requests: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    last_seen_at: str | None = None


class UsageSummary(ApiModel):
    scope: Literal["all", "session", "today"]
    since: str | None = None
    start: str | None = None
    end: str | None = None
    requests: int
    completed: int
    failed: int
    cancelled: int = 0
    duration_ms: float = 0.0
    # completion tokens / predicted_ms over the rows that report timings.
    decode_tps_avg: float | None = None
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_efficiency: float | None = None
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    by_model: list[UsageModelSummary] = Field(default_factory=list)
    top_clients: list[UsageClientSummary] = Field(default_factory=list)


class UsagePoint(ApiModel):
    t: str
    group: str | None = None
    requests: int
    errors: int = 0
    cancelled: int = 0
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int


class UsageTimeseries(ApiModel):
    bucket: Literal["minute", "hour", "day"]
    group_by: Literal["none", "model", "client", "endpoint"]
    start: str
    end: str
    points: list[UsagePoint]
    # 7 × 24 request counts (Monday first, local time) when `view=heatmap`.
    heatmap: list[list[int]] | None = None
    # The same grid in tokens (prompt + completion), for the cell tooltip.
    heatmap_tokens: list[list[int]] | None = None


# Benchmark ------------------------------------------------------------------------

BenchmarkScenario = Literal["decode_short", "cold_prefill", "cached_ttft", "concurrency"]
ALL_SCENARIOS: tuple[BenchmarkScenario, ...] = (
    "decode_short",
    "cold_prefill",
    "cached_ttft",
    "concurrency",
)


class BenchmarkRequest(ApiModel):
    scenarios: list[BenchmarkScenario] = Field(default_factory=lambda: list(ALL_SCENARIOS))
    samples: int = Field(default=3, ge=1, le=10)
    prefill_tokens: list[int] = Field(default_factory=lambda: [2048, 8192, 32768])
    concurrency: list[int] = Field(default_factory=lambda: [1, 2, 3, 4])


class BenchmarkStarted(ApiModel):
    run_id: str


class BenchmarkResult(ApiModel):
    scenario: BenchmarkScenario
    params: dict[str, Any]
    samples: list[dict[str, Any]]
    summary: dict[str, Any]


class BenchmarkRun(ApiModel):
    id: str
    ts: str
    state: Literal["running", "done", "cancelled", "failed"]
    model: str
    revision: str | None = None
    engine_version: str | None = None
    hardware: dict[str, Any]
    power: dict[str, Any]
    settings: dict[str, Any]
    results: list[BenchmarkResult]
    error: str | None = None


class BenchmarkRunSummary(ApiModel):
    id: str
    ts: str
    state: Literal["running", "done", "cancelled", "failed"]
    model: str
    engine_version: str | None = None
    headline: dict[str, float | None] = Field(default_factory=dict)


class BenchmarkRuns(ApiModel):
    runs: list[BenchmarkRunSummary]


class BenchmarkPreflight(ApiModel):
    ready: bool
    warnings: list[str]


class CancelResult(ApiModel):
    cancelled: bool


# Integrations ------------------------------------------------------------------------

CliClient = Literal["claude", "codex", "opencode", "hermes", "pi"]
DesktopApp = Literal["claude-desktop", "codex-app"]


class IntegrationChanges(ApiModel):
    env: dict[str, str] = Field(default_factory=dict)
    args: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CliIntegration(ApiModel):
    name: CliClient
    label: str
    installed: bool
    path: str | None = None
    version: str | None = None
    install_url: str
    command: str
    changes: IntegrationChanges
    entries: list[str] = Field(default_factory=list)
    last_launched_at: str | None = None


IntegrationStep = Literal[
    "backing_up",
    "quitting_app",
    "writing_config",
    "starting_gateway",
    "opening_app",
    "restoring_files",
    "done",
    "failed",
]


class DesktopIntegration(ApiModel):
    name: DesktopApp
    label: str
    detected: bool
    app_path: str | None = None
    version: str | None = None
    untested_version: bool = False
    running: bool = False
    state: Literal["not_connected", "connecting", "connected", "restoring", "needs_restore"]
    connected_at: str | None = None
    warning: str
    # Progress on `integration.state` events while connecting/restoring (null otherwise).
    step: IntegrationStep | None = None
    message: str | None = None


class Integrations(ApiModel):
    cli: list[CliIntegration]
    desktop: list[DesktopIntegration]
    unclean_shutdown: bool = False
    # Set when state.json was unreadable at start (kept at this path).
    corrupt_state: str | None = None


class ConnectRequest(ApiModel):
    confirm_restart: bool = False


class IntegrationError(ApiModel):
    name: str
    message: str


class RestoreAllResult(ApiModel):
    restored: list[str]
    errors: list[IntegrationError] = Field(default_factory=list)


class OpenTerminalRequest(ApiModel):
    model: str | None = None


# CLI shim -------------------------------------------------------------


class RcFileStatus(ApiModel):
    file: str
    present: bool
    managed: bool


class ShimInstallRequest(ApiModel):
    add_to_path: bool = False


class ShimStatus(ApiModel):
    """Whether `splash` is installed, and what is still left to do."""

    shim: str
    installed: bool
    executable: bool
    command: str
    on_path: bool
    rc_files: list[RcFileStatus] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)


class OpenTerminalResult(ApiModel):
    ok: bool
    command: str


class OpenedPath(ApiModel):
    ok: bool = True
    path: str


class PrintedFile(ApiModel):
    path: str
    change: str


class LaunchPrint(ApiModel):
    """What `splash launch <client> --print` reports, as data."""

    client: str
    model: str | None
    exact: bool  # false: Splash's configurator could not run; a static description
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: list[str] = Field(default_factory=list)  # values shown as "••••"
    removed_env: list[str] = Field(default_factory=list)
    args: list[str] = Field(default_factory=list)  # the client argv after the program
    command: str | None = None  # shell form, secrets as "${SPLASH_API_KEY}"
    files: list[PrintedFile] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class EntriesRemoved(ApiModel):
    removed: list[str]
    backups: list[str]


# Logs, traces, diagnostics ---------------------------------------------------------

LogSource = Literal["engine", "manager"]


class LogLine(ApiModel):
    seq: int
    ts: str | None = None
    level: Literal["debug", "info", "warn", "error"] = "info"
    stream: Literal["stdout", "stderr"] | None = None
    text: str


class LogTail(ApiModel):
    source: LogSource
    lines: list[LogLine]


class TraceItem(ApiModel):
    name: str
    path: str
    size_bytes: int
    modified_at: str


class TraceList(ApiModel):
    enabled: bool
    directory: str
    traces: list[TraceItem]


class DeletedBytes(ApiModel):
    deleted_bytes: int


class DiagnosticsBundle(ApiModel):
    generated_at: str
    versions: Versions
    system: SystemInfo | None = None
    settings: dict[str, Any]
    engine: EngineView | None = None
    status: dict[str, Any] | None = None
    engine_log_tail: list[str]


# Data & privacy --------------------------------------------------------------------

DataTarget = Literal["chats", "usage", "logs", "traces", "kv_cache", "responses", "models"]


class DataSize(ApiModel):
    target: DataTarget
    label: str
    bytes: int | None
    items: int | None = None
    note: str | None = None


class DataSizes(ApiModel):
    targets: list[DataSize]


class DataClearRequest(ApiModel):
    target: DataTarget


class DataClearResult(ApiModel):
    target: DataTarget
    freed_bytes: int
    engine_stopped: bool = False
    engine_restarted: bool = False


# Storage --------------------------------------------------------------------------


class StorageInfo(ApiModel):
    models_dir: str
    cache_dir: str
    tmp_dir: str
    splash_data_dir: str
    models_bytes: int | None = None
    cache_bytes: int | None = None
    splash_data_bytes: int | None = None
    free_bytes: int | None = None
    # D44: the models folder resolves to (or nests with) the user's own Hugging Face
    # cache, so a model delete can remove files they downloaded themselves.
    models_shared_with_hf_cache: bool = False
    hf_cache_path: str | None = None


class StorageMoveRequest(ApiModel):
    target: Literal["models", "cache"]
    path: str
    move_files: bool = True


class ImportCandidate(ApiModel):
    repo_id: str
    path: str
    size_bytes: int
    kind: Literal["model", "draft"]
    family: Family | None = None


class ImportCandidates(ApiModel):
    source_dir: str
    candidates: list[ImportCandidate]


class ImportRequest(ApiModel):
    repo_ids: list[str]


# Auth ------------------------------------------------------------------------------


class LoginRequest(ApiModel):
    key: str


class AuthState(ApiModel):
    admin_requires_key: bool
    authenticated: bool
    method: Literal["session", "cli_token", "open"] | None = None


class HelloEvent(ApiModel):
    """First event on /events: a full snapshot, so reconnects need no replay."""

    server_time: str
    engine: EngineView
    alerts: list[Alert]
    downloads: list[DownloadItem]


# Payloads that only travel over SSE; added to the OpenAPI components so generated
# client types include them (see app.custom_openapi).
SSE_MODELS: tuple[type[BaseModel], ...] = (
    HelloEvent,
    EngineView,
    Alert,
    AlertCleared,
    Notification,
    SettingsChangedEvent,
    ModelsChangedEvent,
    EngineUpgradeEvent,
    JobEvent,
    BenchmarkProgressEvent,
    DownloadItem,
    LiveMetrics,
    LogLine,
    LogBackfill,
    ProcessExit,
)

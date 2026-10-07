"""Per-field metadata for the settings form (SPEC §8.2, §8.5, §10.9, Appendix A).

Keys are dotted paths inside `global` (and inside `models[ID]` for scope M):
`serve.max_context`, `server.port`. Secrets kept in the Keychain appear with
`storage="keychain"`; they are not part of settings.json.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Applies = Literal["immediate", "restart", "next_load"]
Scope = Literal["G", "M", "GM"]
Control = Literal[
    "toggle",
    "number",
    "text",
    "select",
    "size",
    "tags",
    "path",
    "url",
    "secret",
    "model",
    "duration",
    "context",
    "flags",
    "json",
]


@dataclass(frozen=True)
class Section:
    id: str
    label: str


SECTIONS: tuple[Section, ...] = (
    Section("server_network", "Server & network"),
    Section("security", "Security"),
    Section("models_storage", "Models & storage"),
    Section("memory_context", "Memory & context"),
    Section("cache", "Cache"),
    Section("performance", "Performance"),
    Section("requests_limits", "Requests & limits"),
    Section("reasoning_sampling", "Reasoning & sampling"),
    Section("routing", "Routing"),
    Section("hugging_face", "Hugging Face"),
    Section("chat_mcp", "Chat & MCP"),
    Section("lifecycle", "Lifecycle"),
    Section("menu_bar", "Menu bar"),
    Section("notifications", "Notifications"),
    Section("data_privacy", "Data & privacy"),
    Section("advanced", "Advanced"),
    Section("about", "About"),
)
SECTION_IDS = frozenset(s.id for s in SECTIONS) | {"integrations", "hidden"}


@dataclass(frozen=True)
class FieldMeta:
    key: str
    label: str
    help: str
    section: str
    control: Control
    applies: Applies = "immediate"
    scope: Scope = "G"
    flag: str | None = None
    env: str | None = None
    advanced: bool = False
    choices: tuple[str, ...] = ()
    min: float | None = None
    max: float | None = None
    unit: str | None = None
    storage: Literal["settings", "keychain"] = "settings"
    # The engine option whose help text and default the schema attaches (SPEC §8.4).
    engine_option: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _serve(
    key: str,
    label: str,
    help: str,
    section: str,
    control: Control,
    flag: str,
    *,
    scope: Scope = "G",
    advanced: bool = False,
    **kw: object,
) -> FieldMeta:
    return FieldMeta(
        key=key,
        label=label,
        help=help,
        section=section,
        control=control,
        flag=flag,
        applies="restart",
        scope=scope,
        advanced=advanced,
        engine_option=flag,
        **kw,  # type: ignore[arg-type]
    )


_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

FIELDS: tuple[FieldMeta, ...] = (
    # Server & network (§8.2) — the manager's public bind, not the engine's.
    FieldMeta(
        "server.host",
        "Listen on",
        "This Mac only (127.0.0.1), the local network "
        "(0.0.0.0) or a specific IP. A LAN bind requires an API key.",
        "server_network",
        "select",
        flag="--host",
        engine_option="--host",
        choices=("127.0.0.1", "0.0.0.0"),  # noqa: S104
    ),
    FieldMeta(
        "server.port",
        "Port",
        "The public port for the API and this admin. Clients "
        "and Splash's launchers follow it through SPLASH_PORT.",
        "server_network",
        "number",
        flag="--port",
        min=1,
        max=65535,
    ),
    FieldMeta(
        "server.allowed_hosts",
        "Allowed hosts",
        "Extra Host names to accept, such as mymac.local. Enforced by Splash GUI.",
        "server_network",
        "tags",
        flag="--allowed-host",
        engine_option="--allowed-host",
    ),
    FieldMeta(
        "server.allowed_origins",
        "Allowed origins",
        "Browser or webview origins that may call the API, such as tauri://localhost; '*' for any.",
        "server_network",
        "tags",
        flag="--allowed-origin",
        engine_option="--allowed-origin",
        warnings=("'*' without an API key lets any web page call the API.",),
    ),
    # Security
    FieldMeta(
        "security.api_key_required",
        "Require API key",
        "Require the key on /v1, /status and /metrics. Forced on for a LAN bind.",
        "security",
        "toggle",
    ),
    FieldMeta(
        "security.api_key",
        "API key",
        "Generate, reveal, copy or rotate. Stored in the macOS Keychain (ai.splashgui.apikey).",
        "security",
        "secret",
        flag="--api-key",
        env="SPLASH_API_KEY",
        storage="keychain",
        engine_option="--api-key",
    ),
    FieldMeta(
        "security.admin_requires_key",
        "Protect the admin",
        "Require the API key to open /admin and /api/admin (12-hour session cookie). On by "
        "default. Turned off, this Mac can still read the admin without signing in, but "
        "every change and every secret still needs a session or the CLI token. On a "
        "local-network bind the cookie travels over plain HTTP; use a TLS reverse proxy.",
        "security",
        "toggle",
    ),
    # Engine (Advanced)
    FieldMeta(
        "engine.path",
        "Engine path",
        "Override the Splash CLI that is found automatically.",
        "advanced",
        "path",
        applies="restart",
        advanced=True,
    ),
    FieldMeta(
        "engine.internal_port",
        "Engine internal port",
        "Loopback port the engine listens on; auto picks a free port in 18000–18999.",
        "advanced",
        "number",
        applies="restart",
        advanced=True,
        min=1,
        max=65535,
    ),
    FieldMeta(
        "engine.extra_flags",
        "Other engine options",
        "Options this version of Splash GUI doesn't know yet, passed to splash serve as written.",
        "advanced",
        "flags",
        applies="restart",
        advanced=True,
    ),
    # Models (Appendix A, scope M)
    _serve(
        "serve.revision",
        "Revision",
        "Branch, tag or commit; a 40-hex commit never moves (pin).",
        "models_storage",
        "text",
        "--revision",
        scope="M",
    ),
    _serve(
        "serve.draft_model",
        "Draft model",
        "Override the speculative draft (advanced).",
        "models_storage",
        "text",
        "--draft-model",
        scope="M",
        advanced=True,
    ),
    _serve(
        "serve.served_model_names",
        "Aliases",
        "Extra API model names (aliases).",
        "models_storage",
        "tags",
        "--served-model-name",
        scope="M",
    ),
    _serve(
        "serve.announce_served_name",
        "Announce alias",
        "Report the first alias in responses (for clients that check the name).",
        "models_storage",
        "toggle",
        "--announce-served-name",
        scope="M",
    ),
    # Storage
    FieldMeta(
        "storage.models_dir",
        "Models directory",
        "Where model weights live (HF_HUB_CACHE). Changing it offers to move the files.",
        "models_storage",
        "path",
        applies="restart",
        env="HF_HUB_CACHE",
    ),
    FieldMeta(
        "storage.cache_dir",
        "Cache directory",
        "Persistent KV cache (--cache-dir) and the session SSD tier (TMPDIR=<dir>/tmp).",
        "models_storage",
        "path",
        applies="restart",
        flag="--cache-dir",
        env="TMPDIR",
        engine_option="--cache-dir",
    ),
    FieldMeta(
        "downloads.parallel",
        "Parallel downloads",
        "How many downloads run at once.",
        "models_storage",
        "number",
        min=1,
        max=3,
    ),
    FieldMeta(
        "downloads.full_verify",
        "Full verify",
        "Hash every file after a download (models.py verify --full). Slower.",
        "models_storage",
        "toggle",
    ),
    # Memory & context
    _serve(
        "serve.language_only",
        "Language only",
        "Skip vision — saves ~0.9 GB and more context; images/PDFs rejected.",
        "memory_context",
        "toggle",
        "--language-only",
        scope="M",
    ),
    _serve(
        "serve.kv_format",
        "KV format",
        "INT8, or BF16 (≈2× KV memory; closer to llama.cpp; can be slower at long context).",
        "memory_context",
        "select",
        "--kv-format",
        scope="GM",
        choices=("int8", "bf16"),
        warnings=("bf16: ≈2× KV memory, can be slower at long context.",),
    ),
    _serve(
        "serve.max_memory",
        "Memory ceiling",
        "Ceiling on Metal allocations (not total process memory). Auto or a size such as 28G.",
        "memory_context",
        "size",
        "--max-memory",
    ),
    _serve(
        "serve.max_context",
        "Context limit",
        "Context limit; Auto = largest that fits. Up to 256K (K = 1024).",
        "memory_context",
        "context",
        "--max-context",
        scope="GM",
        min=4096,
        max=262144,
        unit="tokens",
    ),
    _serve(
        "serve.idle_release",
        "Release weights when idle",
        # D55: the two idle timers, told apart wherever metadata is shown.
        "After this long without a request, Splash frees the model's weights but keeps "
        "its process running; the next request restores them. 10 minutes by default. "
        "Seconds or 90s, 30m, 2h; off keeps them. Lifecycle → Idle unload is the other "
        "timer: it stops the process.",
        "memory_context",
        "text",
        "--idle-release",
    ),
    # Cache
    _serve(
        "serve.max_cache_disk",
        "SSD cache",
        "SSD cache for KV and states. 0 turns it off.",
        "cache",
        "size",
        "--max-cache-disk",
        warnings=(
            "Below about one state (109 MiB for 35B, 187 MiB for 27B) Splash disables the tier.",
        ),
    ),
    _serve(
        "serve.persistent_cache",
        "Persistent cache",
        "Keep cache across restarts, upgrades and crashes (requires SSD cache).",
        "cache",
        "toggle",
        "--persistent-cache",
    ),
    # Performance
    _serve(
        "serve.decode_share",
        "Decode share",
        "Decode time owed per unit of prefill while "
        "others generate. Higher = smoother output for others during long prompts; "
        "0 = alternate.",
        "performance",
        "number",
        "--decode-share",
        scope="GM",
        min=0,
    ),
    _serve(
        "serve.disable_ane",
        "GPU-only prefill",
        "Prefill on the GPU alone. By default a dense model's (27B) long prompts also use "
        "the Neural Engine when that is faster; the 35B MoE always prefills on the GPU.",
        "performance",
        "toggle",
        "--disable-ane",
        scope="GM",
    ),
    _serve(
        "serve.allow_idle_sleep",
        "Let the Mac sleep during requests",
        "By default Splash keeps the Mac awake until running requests finish (the display "
        "may still sleep).",
        "performance",
        "toggle",
        "--allow-idle-sleep",
    ),
    # Requests & limits
    _serve(
        "serve.max_request_size",
        "Max request size",
        "Max HTTP body; shared input budget = max(512M, 2×).",
        "requests_limits",
        "size",
        "--max-request-size",
    ),
    _serve(
        "serve.max_image_pixels",
        "Max image pixels",
        "Max resized pixels per image (vision scratch ≈600 MiB at default).",
        "requests_limits",
        "number",
        "--max-image-pixels",
        scope="GM",
        min=65536,
        max=4194304,
        unit="pixels",
    ),
    _serve(
        "serve.request_timeout",
        "Request timeout",
        "Hard limit from arrival; requests may only shorten it. Off = none.",
        "requests_limits",
        "duration",
        "--request-timeout",
        unit="s",
    ),
    _serve(
        "serve.queue_size",
        "Queue size",
        "Requests admitted at once (running + waiting); more get 503.",
        "requests_limits",
        "number",
        "--queue-size",
        min=1,
    ),
    # Reasoning & sampling
    _serve(
        "serve.default_reasoning_effort",
        "Default reasoning effort",
        "Chat/Responses effort when a request doesn't say. Model default sends nothing.",
        "reasoning_sampling",
        "select",
        "--default-reasoning-effort",
        scope="GM",
        choices=_EFFORTS,
        env="SPLASH_DEFAULT_REASONING_EFFORT",
    ),
    # Routing
    FieldMeta(
        "routing.auto_load",
        "Auto-load on request",
        "A request for an installed model "
        "that isn't loaded switches to it when the engine is idle.",
        "routing",
        "toggle",
    ),
    FieldMeta(
        "routing.switch_when_busy",
        "When busy",
        "Reject (503 + Retry-After) or wait until in-flight requests finish.",
        "routing",
        "select",
        choices=("reject", "wait"),
    ),
    FieldMeta(
        "routing.default_model",
        "Default model",
        "Loaded when a request names no "
        "model or an unknown one (with fallback on), and by splash launch.",
        "routing",
        "model",
    ),
    FieldMeta(
        "routing.unknown_model_fallback",
        "Unknown model fallback",
        "Serve unknown model names with the default model.",
        "routing",
        "toggle",
    ),
    FieldMeta(
        "routing.load_timeout",
        "Load timeout",
        "How long a request waits for a model switch.",
        "routing",
        "number",
        min=1,
        unit="s",
    ),
    # Hugging Face
    FieldMeta(
        "hf.token_override",
        "Hugging Face token",
        "Overrides the hf auth login token. Stored in the Keychain (ai.splashgui.hf).",
        "hugging_face",
        "secret",
        applies="next_load",
        env="HF_TOKEN",
        storage="keychain",
    ),
    FieldMeta(
        "hf.offline",
        "Offline",
        "Start installed models without contacting Hugging Face.",
        "hugging_face",
        "toggle",
        applies="restart",
        flag="--offline",
        env="HF_HUB_OFFLINE",
        engine_option="--offline",
    ),
    FieldMeta(
        "hf.endpoint",
        "Hub endpoint",
        "A Hugging Face mirror URL.",
        "hugging_face",
        "url",
        applies="restart",
        env="HF_ENDPOINT",
        advanced=True,
    ),
    # Chat & MCP
    FieldMeta(
        "chat.mcp_servers",
        "MCP servers",
        "stdio commands or streamable HTTP URLs, in mcp.json format.",
        "chat_mcp",
        "json",
    ),
    # Lifecycle
    FieldMeta(
        "lifecycle.launch_at_login",
        "Launch at login",
        "Start Splash GUI when you log in.",
        "lifecycle",
        "toggle",
    ),
    FieldMeta(
        "lifecycle.stop_on_quit",
        "Stop server when quitting",
        "Quitting the app stops the manager and engine and restores desktop integrations.",
        "lifecycle",
        "toggle",
    ),
    FieldMeta(
        "lifecycle.auto_restart",
        "Auto-restart on crash",
        "Restart after 1 s, 5 s, 30 s; give up after 3 crashes in 5 minutes.",
        "lifecycle",
        "toggle",
    ),
    FieldMeta(
        "lifecycle.idle_unload",
        "Idle unload",
        "Splash GUI stops the engine process after Idle minutes with no API requests, "
        "freeing all its memory; a later request loads the model again when auto-load "
        "is on. Memory & context → Release weights when idle is the other timer: Splash "
        "frees only the weights and keeps the process.",
        "lifecycle",
        "toggle",
    ),
    FieldMeta(
        "lifecycle.idle_unload_minutes",
        "Idle minutes",
        "Minutes without requests before Splash GUI stops the engine process.",
        "lifecycle",
        "number",
        min=5,
        max=240,
        unit="min",
    ),
    # Menu bar
    FieldMeta(
        "menubar.show_tokps",
        "Show tok/s",
        "Live tokens per second while generating.",
        "menu_bar",
        "toggle",
    ),
    FieldMeta(
        "menubar.show_memory",
        "Show memory",
        "MEM mini-gauge (engine Metal current/limit).",
        "menu_bar",
        "toggle",
    ),
    FieldMeta("menubar.show_gpu", "Show GPU", "GPU utilization mini-gauge.", "menu_bar", "toggle"),
    FieldMeta(
        "menubar.show_switcher", "Quick switcher", "Load Model submenu.", "menu_bar", "toggle"
    ),
    # Notifications
    FieldMeta("notifications.download_done", "Download finished", "", "notifications", "toggle"),
    FieldMeta("notifications.engine_failed", "Engine failed", "", "notifications", "toggle"),
    FieldMeta(
        "notifications.memory_critical", "Critical memory pressure", "", "notifications", "toggle"
    ),
    FieldMeta("notifications.update_available", "Update available", "", "notifications", "toggle"),
    FieldMeta(
        "notifications.disk_cache_errors", "Disk cache errors", "", "notifications", "toggle"
    ),
    # Advanced
    FieldMeta(
        "advanced.crash_trace",
        "Crash traces",
        "Record engine crash traces. They can contain private conversation data.",
        "advanced",
        "toggle",
        applies="restart",
        env="SPLASH_CRASH_TRACE",
        advanced=True,
        warnings=("Traces can contain private conversation data.",),
    ),
    # Integrations page
    FieldMeta(
        "integrations.claude_desktop.port",
        "Claude Desktop gateway port",
        "Loopback port for the Claude Desktop gateway while connected.",
        "integrations",
        "number",
        min=1024,
        max=65535,
    ),
    FieldMeta(
        "integrations.claude_desktop.slots",
        "Claude model slots",
        "Which Splash model or profile each Claude model slot uses; empty = the active model.",
        "integrations",
        "json",
    ),
    FieldMeta(
        "integrations.codex_app.make_default",
        "Make Splash the default in Codex app",
        "Also set Codex app's default model to the Splash model.",
        "integrations",
        "toggle",
    ),
    # Not shown on the Settings page
    FieldMeta(
        "ui.theme",
        "Theme",
        "Light, dark or follow the system.",
        "hidden",
        "select",
        choices=("light", "dark", "system"),
    ),
    FieldMeta("wizard.completed", "Wizard completed", "", "hidden", "toggle"),
    FieldMeta(
        "wizard.preset",
        "Wizard preset",
        "",
        "hidden",
        "select",
        choices=("coding", "chat", "speed"),
    ),
)

FIELDS_BY_KEY: dict[str, FieldMeta] = {f.key: f for f in FIELDS}
MODEL_SCOPED_KEYS: tuple[str, ...] = tuple(f.key for f in FIELDS if f.scope in ("M", "GM"))
GLOBAL_KEYS: tuple[str, ...] = tuple(
    f.key for f in FIELDS if f.scope in ("G", "GM") and f.storage == "settings"
)

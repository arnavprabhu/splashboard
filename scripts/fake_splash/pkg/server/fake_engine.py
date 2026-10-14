"""The simulated engine behind the fake server: memory plan and startup
lines, request scheduling and generation timing, failure modes, counters and
the /status document (schema 6).

Field names follow runtime/engine/Status.cpp, server/backend.py
(`transport`), server/frontend.py (`vision`, `input_modalities`,
`chat_template`, `frontend`, caches, `latency`) and server/server.py
(`instance`, `http`). Values are invented but internally consistent.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import zlib
from collections import OrderedDict, deque
from collections.abc import Generator, Iterator
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import IO, Any

from . import fake_text
from .diagnostics import print_request
from .errors import APIError
from .fake_shapes import CacheInfo, FakeJob, NativeResult
from .latency import LatencyMetrics
from .protocol import MAX_THINKING_END_TOKENS

KIB, MIB, GIB = 1 << 10, 1 << 20, 1 << 30
STATUS_SCHEMA_VERSION = 6  # runtime/engine/Protocol.hpp kStatusSchemaVersion
HEAD_DIMENSION = 256
KV_PAGE_TOKENS = 32  # runtime/metal/abi/ExecutionGeometry.h SPLASH_TARGET_KV_BLOCK_TOKENS
PREFILL_TOKEN_BUDGET = 2048  # SPLASH_PREFILL_TOKEN_BUDGET
MAX_BATCH_WIDTH = 4  # model::ExecutionLimits::maximumBatchWidth
PIPELINE_RESERVE = 256 * MIB  # runtime/model/Model.hpp kPipelineReserveBytes
RUNTIME_RESERVE = 512 * MIB  # kRuntimeOverheadReserveBytes
SPECULATIVE_SCRATCH_TOKENS = 64
MAX_CONTEXT_TOKENS = 262144
# runtime/engine/DiskSpace.hpp kAutomaticCacheDiskBytes and DiskSpace.cpp kFloorBytes (1.3.2).
AUTOMATIC_CACHE_DISK = 32 * (1 << 30)
DISK_FLOOR = 10 * (1 << 30)
# The mount point DiskVolume reports for the user's data volume.
DATA_VOLUME = "/System/Volumes/Data"
# The Neural Engine FFN split (1.3.0) as the real engine set it up for the 27B on
# the user's M5 Pro (Evidence): its share,
# least chunk and memory. The MoE has no dense FFN layers and never splits.
ANE_SHARE = 0.3235294118
ANE_MINIMUM_ROWS = 524
ANE_FFN_BYTES = 229_703_680
ANE_LAYERS = 64
# runtime/engine/AneFfnStartup.cpp decide(): each outcome's reason.
ANE_SPLIT_REASON = (
    "at share 0.32 for chunks of 524 rows or more, 2.2% from the GPU alone on the "
    "Neural Engine's part (set up as calibrated before in 2.0 s)"
)
ANE_OFF_GIVEN = "as given"
ANE_NOT_DENSE = "the target has no dense FFN layers"

MODES = (
    "normal",
    "engine_recovering",
    "engine_failed",
    "queue_full",
    "capacity_exhausted",
    "resource_timeout",
    "fail_midstream",
)
PRESSURES = ("normal", "warning", "critical")
TEMPLATES = ("native", "patched", "unsupported")

# server/chat_templates.py _DESCRIPTIONS and _generation_description.
_TEMPLATE_DESCRIPTIONS = {
    "native": "renders later system messages in place",
    "patched": "patched to render later system messages in place (the original template rejects them)",
    "unsupported": "requests with later system messages are rejected (the original template misplaces them)",
}
# server/chat_templates.py LATER_SYSTEM_UNSUPPORTED
LATER_SYSTEM_UNSUPPORTED = "this model's chat template does not accept system messages after the first message"


def now_hms() -> str:
    return time.strftime("%H:%M:%S")


def print_status(message: str, *, error: bool = False) -> None:
    """server/diagnostics.py print_status: one timestamped write per line,
    stdout unless error."""
    stream = sys.stderr if error else sys.stdout
    stream.write(f"{now_hms()} {message}\n")
    stream.flush()


def log_startup(message: str) -> None:
    """runtime/engine/StartupLog.hpp logStartup: timestamped, on stderr."""
    sys.stderr.write(f"{now_hms()} {message}\n")
    sys.stderr.flush()


def write_stderr_line(message: str) -> None:
    """runtime/StderrLine.hpp writeStderrLine: no timestamp."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMG]?)(?:I?B)?\s*$", re.IGNORECASE)


def parse_size(text: str | None, default: int) -> int:
    if not text:
        return default
    match = _SIZE.match(text)
    if match is None:
        return default
    return int(float(match.group(1)) * 1024 ** " KMG".index(match.group(2).upper() or " "))


def gibibytes(value: int) -> str:
    """runtime/engine/DiskSpace.cpp gibibytes."""
    return f"{value / GIB:.1f} GiB"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.lower() not in ("0", "false", "no", "off")


@dataclass
class FakeConfig:
    """Behaviour knobs. Each reads FAKE_SPLASH_<NAME upper> at startup and
    can be changed live with POST /_fake/mode {"config": {...}}."""

    tokens_per_second: float = 200.0
    prefill_tokens_per_second: float = 4000.0
    load_seconds: float = 0.2
    start_delay: float = 0.0
    restore_seconds: float = 0.3
    idle_release_seconds: float = 600.0
    keepalive_seconds: float = 2.0
    flush_seconds: float = 0.5
    text: str | None = None
    reply_tokens: int | None = None
    reasoning: str = "auto"  # auto | always | never
    template: str = "patched"
    memory_pressure: str = "normal"
    thermal_state: str = "nominal"  # nominal | fair | serious | critical (1.3.1)
    physical_memory: int = 64 * GIB
    host_available: int = 40 * GIB
    auto_context: int | None = None
    # The volume the SSD cache's files are on (runtime/engine/DiskSpace.cpp,
    # 1.3.2): its free space sizes the tier's limit and decides whether the
    # automatic tier opens at all.
    volume_available: int = 200 * GIB
    volume_size: int = 994 * GIB
    writes_gated: str = "no"  # no | budget | closed (Status.cpp writes_gated)
    startup_failure: str = ""  # "" | budget | context | crash
    crash_code: int = 1
    listen_early: bool = False
    gpu_family: int = 10
    gpu_cores: int = 20

    @classmethod
    def from_env(cls) -> FakeConfig:
        config = cls()
        env = os.environ
        for name, default in (
            ("tokens_per_second", "FAKE_SPLASH_TOKS"),
            ("prefill_tokens_per_second", "FAKE_SPLASH_PREFILL_TPS"),
            ("load_seconds", "FAKE_SPLASH_LOAD_SECONDS"),
            ("start_delay", "FAKE_SPLASH_START_DELAY"),
            ("restore_seconds", "FAKE_SPLASH_RESTORE_SECONDS"),
            ("idle_release_seconds", "FAKE_SPLASH_IDLE_RELEASE_SECONDS"),
            ("keepalive_seconds", "FAKE_SPLASH_KEEPALIVE_SECONDS"),
            ("flush_seconds", "FAKE_SPLASH_FLUSH_SECONDS"),
        ):
            setattr(config, name, _env_float(default, getattr(config, name)))
        config.text = env.get("FAKE_SPLASH_TEXT") or None
        if env.get("FAKE_SPLASH_REPLY_TOKENS"):
            config.reply_tokens = int(env["FAKE_SPLASH_REPLY_TOKENS"])
        config.reasoning = env.get("FAKE_SPLASH_REASONING", config.reasoning)
        config.template = env.get("FAKE_SPLASH_TEMPLATE", config.template)
        config.memory_pressure = env.get("FAKE_SPLASH_MEMORY_PRESSURE", config.memory_pressure)
        config.thermal_state = env.get("FAKE_SPLASH_THERMAL_STATE", config.thermal_state)
        config.physical_memory = parse_size(env.get("FAKE_SPLASH_PHYSICAL_MEMORY"), config.physical_memory)
        config.host_available = parse_size(env.get("FAKE_SPLASH_HOST_AVAILABLE"), config.host_available)
        if env.get("FAKE_SPLASH_AUTO_CONTEXT"):
            config.auto_context = int(env["FAKE_SPLASH_AUTO_CONTEXT"])
        config.volume_available = parse_size(env.get("FAKE_SPLASH_VOLUME_AVAILABLE"), config.volume_available)
        config.volume_size = parse_size(env.get("FAKE_SPLASH_VOLUME_SIZE"), config.volume_size)
        config.writes_gated = env.get("FAKE_SPLASH_WRITES_GATED", config.writes_gated)
        config.startup_failure = env.get("FAKE_SPLASH_FAIL_STARTUP", "")
        config.crash_code = int(env.get("FAKE_SPLASH_CRASH_CODE", config.crash_code))
        config.listen_early = _env_bool("FAKE_SPLASH_LISTEN_EARLY", False)
        return config

    def update(self, values: dict) -> None:
        known = {f.name: f for f in fields(self)}
        for key, value in values.items():
            if key not in known:
                raise APIError(400, f"unknown fake config field: {key}")
            current = getattr(self, key)
            if isinstance(current, bool) and not isinstance(value, bool):
                raise APIError(400, f"{key} must be a boolean")
            if isinstance(current, (int, float)) and not isinstance(current, bool) and value is not None:
                value = type(current)(value)
            setattr(self, key, value)


@dataclass
class ServeSettings:
    """What `splash serve` was started with, as the fake uses it."""

    model: str
    host: str
    port: int
    max_context: int | None
    max_memory: int | None
    # --max-cache-disk: None for auto (1.3.2's default), 0 for off, else bytes.
    max_cache_disk: int | None
    persistent_cache: bool
    cache_dir: Path | None
    kv_format: str
    language_only: bool
    queue_size: int
    max_request_size: int
    request_timeout: float | None
    served_model_names: list[str]
    announce_served_name: bool
    default_reasoning_effort: str | None
    decode_share: float | None
    max_image_pixels: int
    # --idle-release in seconds, math.inf for off, None for the default (FakeConfig's).
    idle_release: float | None = None
    disable_ane: bool = False
    allow_idle_sleep: bool = False
    # 1.3.2's --max-cache-disk-writes (bytes a day, 0 for no limit),
    # --state-format, --thinking-token-budget and --thinking-end-text.
    max_cache_disk_writes: int = 0
    state_format: str = "fp32"
    thinking_token_budget: int | None = None
    thinking_end_text: str | None = None

    @property
    def cache_disk_quota(self) -> int:
        """The tier's quota: 32 GiB for auto (DiskSpace.hpp
        kAutomaticCacheDiskBytes), 0 for off."""
        return AUTOMATIC_CACHE_DISK if self.max_cache_disk is None else self.max_cache_disk

    @property
    def ane_split(self) -> bool:
        """Whether the Neural Engine takes part of the prefill FFN: a dense target
        without --disable-ane (AneFfnStartup.cpp decide)."""
        return _family(self.model) == "Qwen3.8-27B" and not self.disable_ane


def _family(model: str) -> str:
    return "Qwen3.6-35B-A3B" if "35b-a3b" in model.lower() else "Qwen3.8-27B"


@dataclass
class Breakdown:
    """runtime/engine/MemoryPlan.cpp EngineMemoryBreakdown."""

    physical_memory_bytes: int
    recommended_working_set_bytes: int
    configured_memory_limit_bytes: int
    working_set_margin_bytes: int
    hard_budget_bytes: int
    target_weights_bytes: int
    draft_weights_bytes: int
    vision_weights_bytes: int
    maximum_batch_width: int
    lane_state_bytes: int
    shared_prefill_bytes: int
    shared_decode_bytes: int
    pipeline_reserve_bytes: int
    runtime_overhead_reserve_bytes: int
    ane_ffn_bytes: int
    state_staging_bytes: int
    fixed_runtime_bytes: int
    dynamic_budget_bytes: int
    kv_page_tokens: int
    kv_page_bytes: int
    kv_extent_pages: int
    kv_extent_bytes: int
    kv_capacity_pages: int
    kv_capacity_bytes: int
    kv_capacity_tokens: int
    minimum_dynamic_bytes: int
    minimum_required_bytes: int
    deficit_bytes: int

    @staticmethod
    def _mib(value: int) -> str:
        # MemoryPlan.cpp bytesAndMiB
        return f"{value} bytes ({value / MIB:.2f} MiB)"

    def describe(self) -> str:
        """EngineMemoryBreakdown::describe, line for line."""
        m = self._mib
        limit = m(self.configured_memory_limit_bytes) if self.configured_memory_limit_bytes else "automatic"
        return "\n".join(
            [
                f"physical memory: {m(self.physical_memory_bytes)}",
                f"recommended working set: {m(self.recommended_working_set_bytes)}",
                f"configured memory limit: {limit}",
                f"working-set margin (max of 1 GiB or 2%): {m(self.working_set_margin_bytes)}",
                f"hard budget: {m(self.hard_budget_bytes)}",
                f"target weights: {m(self.target_weights_bytes)}",
                f"draft weights: {m(self.draft_weights_bytes)}",
                f"vision weights: {m(self.vision_weights_bytes)}",
                f"maximum DFlash batch width: {self.maximum_batch_width}",
                f"lane state: {m(self.lane_state_bytes)}",
                f"shared prefill: {m(self.shared_prefill_bytes)}",
                f"shared decode: {m(self.shared_decode_bytes)}",
                f"pipeline reserve: {m(self.pipeline_reserve_bytes)}",
                f"allocator/runtime reserve: {m(self.runtime_overhead_reserve_bytes)}",
                f"Neural Engine split: {m(self.ane_ffn_bytes)}",
                f"SSD cache state staging: {m(self.state_staging_bytes)}",
                f"fixed runtime: {m(self.fixed_runtime_bytes)}",
                f"elastic state/KV budget: {m(self.dynamic_budget_bytes)}",
                f"KV page: {self.kv_page_tokens} tokens, {m(self.kv_page_bytes)}",
                f"KV extent: {self.kv_extent_pages} pages, {m(self.kv_extent_bytes)}",
                f"KV capacity of one request: {self.kv_capacity_pages} pages / {self.kv_capacity_tokens} tokens",
                f"minimum dynamic runtime: {m(self.minimum_dynamic_bytes)}",
                f"minimum required: {m(self.minimum_required_bytes)}",
                f"deficit: {m(self.deficit_bytes)}",
            ]
        )


class MemoryPlan:
    def __init__(self, settings: ServeSettings, config: FakeConfig, *, force_refusal: bool = False):
        family = _family(settings.model)
        moe = family == "Qwen3.6-35B-A3B"
        self.family = family or "Qwen3.8-27B"
        # Full-attention KV layout: one layer in four, head_dim 256.
        self.attention_layers, self.kv_heads = (10, 2) if moe else (16, 4)
        self.kv_format = settings.kv_format
        physical = config.physical_memory
        recommended = int(physical * 0.75)
        margin = max(GIB, recommended * 2 // 100)
        configured = settings.max_memory or 0
        ceiling = min(configured, recommended) if configured else recommended
        hard = max(0, ceiling - margin)
        target = (19_600 if moe else 15_200) * MIB
        draft = (980 if moe else 1_900) * MIB
        vision = 0 if settings.language_only else 860 * MIB
        lane_state = self.lane_state_bytes(settings)
        prefill, decode = 1_536 * MIB, 512 * MIB
        # RuntimeResources.mm (1.3.2): a quota that holds a state, the automatic
        # one included, sets a state's staging aside.
        staging = lane_state if settings.cache_disk_quota >= lane_state else 0
        self.state_format = settings.state_format
        ane = ANE_FFN_BYTES if settings.ane_split else 0
        fixed = target + draft + vision + prefill + decode + PIPELINE_RESERVE + RUNTIME_RESERVE + staging + ane
        dynamic = max(0, hard - fixed)
        # Full-attention KV per token: layers x KV heads x head_dim x (K, V).
        per_token = self.attention_layers * self.kv_heads * HEAD_DIMENSION * 2
        if settings.kv_format == "bf16":
            per_token *= 2
        page = per_token * KV_PAGE_TOKENS
        extent_pages = 64
        minimum_dynamic = lane_state + 2 * extent_pages * page
        required = fixed + minimum_dynamic
        capacity_pages = max(0, (dynamic - lane_state * MAX_BATCH_WIDTH) // page)
        refused = force_refusal or dynamic < minimum_dynamic
        self.breakdown = Breakdown(
            physical,
            recommended,
            configured,
            margin,
            hard,
            target,
            draft,
            vision,
            MAX_BATCH_WIDTH,
            lane_state,
            prefill,
            decode,
            PIPELINE_RESERVE,
            RUNTIME_RESERVE,
            ane,
            staging,
            fixed,
            dynamic,
            KV_PAGE_TOKENS,
            page,
            extent_pages,
            extent_pages * page,
            capacity_pages,
            capacity_pages * page,
            capacity_pages * KV_PAGE_TOKENS,
            minimum_dynamic,
            required,
            max(0, required - hard) if refused else 0,
        )
        if force_refusal and self.breakdown.deficit_bytes == 0:
            self.breakdown.deficit_bytes = page * extent_pages
        self.refused = refused
        automatic = min(MAX_CONTEXT_TOKENS, max(0, self.breakdown.kv_capacity_tokens - SPECULATIVE_SCRATCH_TOKENS))
        if config.auto_context is not None:
            automatic = min(automatic or config.auto_context, config.auto_context)
        self.automatic_context = automatic
        self.page_bytes = page

    @staticmethod
    def lane_state_bytes(settings: ServeSettings) -> int:
        """One lane's state; --state-format bf16 (1.3.2) halves each recurrent state."""
        state = (96 if _family(settings.model) == "Qwen3.6-35B-A3B" else 160) * MIB
        return state // 2 if settings.state_format == "bf16" else state

    def status_json(self, valid: bool = True) -> dict:
        b = self.breakdown
        budget = asdict(b)
        if valid:
            return {
                "valid": True,
                "maximum_context_tokens": self.automatic_context,
                "device": {
                    "device_name": "Apple M5 Pro (fake)",
                    "macos_version": "27.0.0",
                    "apple_gpu_family": 10,
                    "gpu_core_count": 20,
                    "physical_memory_bytes": self.breakdown.physical_memory_bytes,
                    "recommended_max_working_set_bytes": self.breakdown.recommended_working_set_bytes,
                    "max_buffer_length_bytes": self.breakdown.recommended_working_set_bytes,
                    "max_threadgroup_memory_bytes": 32768,
                    "max_threadgroup_width": 1024,
                    "has_unified_memory": True,
                },
                # runtime/engine/MemoryPlan.cpp modelStatusJson
                "model": {
                    "model_name": self.family,
                    "maximum_context_tokens": MAX_CONTEXT_TOKENS,
                    "attention_layers": self.attention_layers,
                    "kv_heads": self.kv_heads,
                    "head_dimension": HEAD_DIMENSION,
                    "kv_page_tokens": KV_PAGE_TOKENS,
                    "kv_format": self.kv_format,
                    "kv_elements_per_scale": HEAD_DIMENSION if self.kv_format == "int8" else 0,
                    "kv_page_bytes": self.page_bytes,
                    "state_format": self.state_format,
                    "memory": {
                        "target_weights_bytes": b.target_weights_bytes,
                        "draft_weights_bytes": b.draft_weights_bytes,
                        "vision_weights_bytes": b.vision_weights_bytes,
                        "lane_state_bytes": b.lane_state_bytes,
                        "shared_prefill_bytes": b.shared_prefill_bytes,
                        "shared_decode_bytes": b.shared_decode_bytes,
                        "pipeline_reserve_bytes": b.pipeline_reserve_bytes,
                        "runtime_overhead_reserve_bytes": b.runtime_overhead_reserve_bytes,
                        "ane_ffn_bytes": b.ane_ffn_bytes,
                        "state_staging_bytes": b.state_staging_bytes,
                    },
                },
                "budget": budget,
            }
        # BudgetValidationStatus::toStatusJson
        return {
            "schema_version": 2,
            "valid": False,
            "error_code": "kv_pool_does_not_fit",
            "message": "hard budget cannot fit one lane's state and the KV runway",
            "budget": budget,
        }


class StartupFailed(Exception):
    """Startup ended before Ready; the server prints and exits 1."""


class RequestCancelled(Exception):
    pass


class HttpAdmission:
    """server/server.py HttpAdmission: nonwaiting capacity gate."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.active = 0
        self.lock = threading.Lock()

    def acquire(self, amount: int = 1) -> bool:
        with self.lock:
            if self.active + amount > self.capacity:
                return False
            self.active += amount
            return True

    def release(self, amount: int = 1) -> None:
        with self.lock:
            self.active = max(0, self.active - amount)

    def stats(self) -> dict:
        with self.lock:
            return {"active": self.active, "capacity": self.capacity}


class ResponseStore:
    """server/frontend.py ResponseStore (64 MiB byte-budgeted LRU)."""

    BUDGET_BYTES = 64 * MIB

    def __init__(self):
        self.records: OrderedDict[str, tuple[bytes, bytes]] = OrderedDict()
        self.bytes = 0
        self.evictions = self.hits = self.misses = 0
        self.lock = threading.Lock()

    def get(self, response_id: str) -> tuple[dict, list] | None:
        with self.lock:
            record = self.records.pop(response_id, None)
            if record is None:
                self.misses += 1
                return None
            self.records[response_id] = record
            self.hits += 1
        return json.loads(record[0]), json.loads(record[1])

    def put(self, response: dict, history: list) -> bool:
        record = (json.dumps(response).encode(), json.dumps(history).encode())
        size = len(record[0]) + len(record[1])
        if size > self.BUDGET_BYTES:
            return False
        with self.lock:
            previous = self.records.pop(response["id"], None)
            if previous is not None:
                self.bytes -= len(previous[0]) + len(previous[1])
            self.records[response["id"]] = record
            self.bytes += size
            while self.bytes > self.BUDGET_BYTES:
                _, evicted = self.records.popitem(last=False)
                self.bytes -= len(evicted[0]) + len(evicted[1])
                self.evictions += 1
        return True

    def delete(self, response_id: str) -> bool:
        with self.lock:
            record = self.records.pop(response_id, None)
            if record is None:
                return False
            self.bytes -= len(record[0]) + len(record[1])
            return True

    def stats(self) -> dict:
        with self.lock:
            return {
                "entries": len(self.records),
                "bytes": self.bytes,
                "budget_bytes": self.BUDGET_BYTES,
                "evictions": self.evictions,
                "hits": self.hits,
                "misses": self.misses,
            }


def _percentile(samples: deque, fraction: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return float(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))])


class PersistentCache:
    """A stand-in for CacheDirectory: a namespace folder with a lock and a
    record of the restore points the last clean stop left."""

    def __init__(self, root: Path, namespace: str):
        self.directory = root / namespace
        self.lock_file: IO[str] | None = None
        self.taken_back = {"states": 0, "kv_blocks": 0, "bytes": 0, "left_behind": 0}
        self.unclean = False

    def open(self) -> bool:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = (self.directory / ".lock").open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self.lock_file = handle
        record = self.directory / "restore-points.json"
        if record.exists():
            try:
                saved = json.loads(record.read_text())
                self.taken_back = {k: int(saved.get(k, 0)) for k in self.taken_back}
                self.unclean = not saved.get("clean", False)
            except (OSError, ValueError):
                self.unclean = True
        return True

    def begin_serving(self) -> None:
        """RuntimeResources::beginServing: marked serving only once Ready, so
        a start that fails earlier leaves the last clean stop's record."""
        self._write(clean=False, **self.taken_back)

    def _write(self, *, clean: bool, **counts: int) -> None:
        path = self.directory / "restore-points.json"
        path.write_text(json.dumps({**counts, "clean": clean}))

    def close(self, states: int, blocks: int, nbytes: int) -> None:
        self._write(clean=True, states=states, kv_blocks=blocks, bytes=nbytes, left_behind=0)


class FakeEngine:
    def __init__(self, settings: ServeSettings, config: FakeConfig):
        self.settings = settings
        self.config = config
        self.lock = threading.RLock()
        self.mode = os.environ.get("FAKE_SPLASH_MODE", "normal") or "normal"
        if self.mode not in MODES:
            self.mode = "normal"
        self.engine_error: str | None = None
        self.fatal_error: str | None = None
        self.closing = False
        self.ready = False
        # server.py main() creates the backend after the chat-template line;
        # from then on its cleanup prints "Stopping ·", even after a failed start.
        self.backend_created = False
        self.restarts = 0
        self.started_at = time.time()
        self.instance_id = secrets.token_hex(12)
        self.plan: MemoryPlan | None = None
        self.max_context = 0
        self.vision = not settings.language_only
        self.lanes = threading.Semaphore(MAX_BATCH_WIDTH)
        self.requests = HttpAdmission(settings.queue_size)
        self.token_counts = HttpAdmission(settings.queue_size)
        self.request_bodies = HttpAdmission(max(512 * MIB, 2 * settings.max_request_size))
        self.connections = HttpAdmission(settings.queue_size + 64)
        self.response_store = ResponseStore()
        self.latencies = LatencyMetrics()
        self.persistent: PersistentCache | None = None
        self.build_id = hashlib.sha256(b"splash-1.3.2-fake").hexdigest()[:16]
        self.weights_released = False
        # Status.cpp appendWeights (1.3.2): a restore leaves the weights released
        # until it is done.
        self.weights_restoring = False
        self.weights_restores = 0
        # Whether the SSD cache opened, and the limit its volume left it.
        self.disk_open = False
        self.disk_limit = 0
        # Status.cpp active_requests (1.3.2): the engine's requests in service, by id.
        self.request_ids = 0
        self.active: dict[int, dict] = {}
        # Status.cpp appendAneFfn counters; the warmup runs one split command over
        # every layer, as the real engine's does.
        split = settings.ane_split
        self.ane = {"split_commands": int(split), "ane_ms": 1258.4 if split else 0.0, "evaluations": ANE_LAYERS * split}
        self.last_request = time.monotonic()
        self.prefix_cache: OrderedDict[tuple[int, ...], None] = OrderedDict()
        self.kill_requested = threading.Event()
        # Counters, as Status.cpp names them.
        self.c: dict[str, Any] = {
            "submitted": 0,
            "completed": 0,
            "cancelled": 0,
            "failed": 0,
            "queued": 0,
            "prefilling": 0,
            "decoding": 0,
            "prefill_batches": 0,
            "prefill_rows": 0,
            "decode_batches": 0,
            "b1": 0,
            "b2": 0,
            "b3": 0,
            "b4": 0,
            "prefill_input_tokens": 0,
            "prefill_wall_ms": 0.0,
            "decode_output_tokens": 0,
            "decode_wall_ms": 0.0,
            "drafted_tokens": 0,
            "accepted_draft_tokens": 0,
            "capacity_failures": 0,
            "cache_hits": 0,
            "cold_misses": 0,
            "kv_hit_tokens": 0,
            "reused_tokens": 0,
            "publications": 0,
            "evictions": 0,
            "image_encodes": 0,
            "denied_reservations": 0,
            "resource_suspensions": 0,
            "kv_demotions": 0,
            "kv_restores": 0,
            "disk_written_bytes": 0,
            "disk_read_bytes": 0,
            "durable": 0,
            "active_tokens": 0,
            "max_tick_ms": 0.0,
            "oldest_wait_since": None,
        }
        self.ttft_samples: deque[float] = deque(maxlen=512)
        self.itl_samples: deque[float] = deque(maxlen=4096)
        self.peak_bytes = 0
        self.tokenizer_cache = {"entries": 0, "hits": 0, "reused_tokens": 0, "bytes": 0}

    # --- model names (server/frontend.py) --------------------------------
    @property
    def response_model(self) -> str:
        s = self.settings
        return s.served_model_names[0] if s.announce_served_name else s.model

    @property
    def model_names(self) -> tuple[str, ...]:
        s = self.settings
        return tuple(dict.fromkeys((self.response_model, s.model, *s.served_model_names)))

    @property
    def input_modalities(self) -> list[str]:
        return ["text", "image", "pdf"] if self.vision else ["text"]

    def accepts_model(self, model: object) -> bool:
        """frontend.py accepts_model (1.3.2): a name the server answers to, or
        none, absent or empty, which means the served model."""
        return model in (None, "") or (isinstance(model, str) and model in self.model_names)

    # --- startup -----------------------------------------------------------
    def template_description(self) -> str:
        template = self.config.template if self.config.template in TEMPLATES else "patched"
        return f"{_TEMPLATE_DESCRIPTIONS[template]}, generation prompt 3-7 tokens"

    def start(self) -> None:
        """Print the startup lines in the engine's order and settle the plan;
        raises StartupFailed with the lines already printed."""
        s, cfg = self.settings, self.config
        print_status(f"Loading · {s.model}")  # server/server.py:1747
        # server/server.py:1752 thinking_end_tokens (backend.py:51-74), before
        # the chat template line; it ends the start before any backend exists.
        if s.thinking_end_text is not None:
            tokens = fake_text.tokenize(s.thinking_end_text)
            if not s.thinking_end_text.endswith("</think>"):
                raise StartupFailed("--thinking-end-text must end with </think> and hold no other special token")
            if len(tokens) > MAX_THINKING_END_TOKENS:
                raise StartupFailed(
                    f"--thinking-end-text takes {len(tokens)} tokens; the most is {MAX_THINKING_END_TOKENS}"
                )
        print_status(f"Chat template · {self.template_description()}")  # server.py:1754
        self.backend_created = True
        quota, state_bytes = s.cache_disk_quota, MemoryPlan.lane_state_bytes(s)
        if 0 < quota < state_bytes:
            # runtime/engine/RuntimeResources.mm:398-400 (1.3.2)
            log_startup(
                f"SSD cache off: its quota holds no state of {state_bytes // MIB} MiB; no state staging is set aside."
            )
        time.sleep(max(0.0, cfg.load_seconds))
        # runtime/engine/RuntimeResources.mm:385
        log_startup(f"Weights loaded in {cfg.load_seconds:.2f} s.")
        plan = MemoryPlan(s, cfg, force_refusal=cfg.startup_failure == "budget")
        self.plan = plan
        if plan.refused:
            # RuntimeResources.mm:421 (MemoryPlanning) wrapped by Bootstrap.mm:25,
            # printed by runtime/main.mm:429-431 printBootstrapError.
            message = "hard budget cannot fit one lane's state and the KV runway"
            write_stderr_line(
                f"error: runtime bootstrap failed [resource_assembly]: {message}\n"
                f"engine memory budget validation failed [kv_pool_does_not_fit]: {message}\n"
                + plan.breakdown.describe()
            )
            write_stderr_line("memory_plan_json: " + json.dumps(plan.status_json(valid=False), separators=(",", ":")))
            raise StartupFailed("native protocol reached EOF")
        if cfg.startup_failure == "crash":
            write_stderr_line("error: native runtime I/O failed: fake startup crash")
            raise StartupFailed("native protocol reached EOF")
        log_startup(
            f"Kernel policy for GPU family {cfg.gpu_family} with {cfg.gpu_cores} cores."
        )  # RuntimeResources.mm:452
        # runtime/engine/AneFfnStartup.cpp decide() and logOutcome(); the MoE logs nothing.
        if s.ane_split:
            log_startup("Setting up the Neural Engine FFN split (splash serve --disable-ane keeps the FFN on the GPU).")
            log_startup(f"Neural Engine FFN split {ANE_SPLIT_REASON}.")
        elif _family(s.model) == "Qwen3.8-27B":
            log_startup(f"The GPU runs the prefill FFN alone, {ANE_OFF_GIVEN}.")
        if plan.breakdown.state_staging_bytes:
            self._open_disk_tier(plan)
        automatic = plan.automatic_context
        if (s.max_context is not None and s.max_context > automatic) or cfg.startup_failure == "context":
            requested = s.max_context or automatic + 1
            budget = plan.breakdown
            capped = (
                bool(budget.configured_memory_limit_bytes)
                and budget.hard_budget_bytes + budget.working_set_margin_bytes == budget.configured_memory_limit_bytes
            )
            # runtime/engine/Bootstrap.mm:274-279
            write_stderr_line(
                f"error: runtime bootstrap failed [model_creation]: --max-context {requested} exceeds the "
                f"{automatic} tokens the model and {'--max-memory' if capped else "this Mac's memory"} allow; "
                f"omit it or pass at most {automatic}\n" + budget.describe()
            )
            raise StartupFailed("native protocol reached EOF")
        # 1.3.2 dropped Bootstrap.mm's "may not hold" suggestion: the SSD cache
        # is on by default.
        self.max_context = s.max_context or automatic
        time.sleep(max(0.0, cfg.start_delay))
        if self.persistent is not None:
            self.persistent.begin_serving()
        self.ready = True
        self.last_request = time.monotonic()

    def ready_line(self, address: str) -> str:
        # server/server.py:2017-2023
        context = self.max_context
        text = f"{context // 1024}K" if context % 1024 == 0 else f"{context:,}"
        mode = "" if self.vision else " · language only"
        return f"Ready · {self.settings.model} · context {text}{mode} · {address}"

    def _open_disk_tier(self, plan: MemoryPlan) -> None:
        """RuntimeResources.mm (1.3.2, lines 600-700) and DiskSpace.cpp: the
        automatic tier opens only where its volume's limit holds two states and
        1 GiB of KV; a persistent tier keeps its files in a cache directory of
        its own, else temporary files; the volume's free space sizes the limit."""
        s, cfg = self.settings, self.config
        automatic = s.max_cache_disk is None
        state_bytes = plan.breakdown.lane_state_bytes
        floor = min(DISK_FLOOR, cfg.volume_size // 20)

        def room(held: int) -> int:
            # DiskSpace.cpp diskRoomBytes
            total = cfg.volume_available + held
            return total - floor if total > floor else 0

        def automatic_refusal(held: int) -> str | None:
            # DiskSpace.cpp automaticDiskRefusal
            if min(AUTOMATIC_CACHE_DISK, room(held) // 4) < 2 * state_bytes + GIB:
                return (
                    f"{gibibytes(cfg.volume_available)} available on {DATA_VOLUME} is too little beyond "
                    f"the {gibibytes(floor)} it keeps free"
                )
            return None

        temporary_refusal = root_refusal = None
        if automatic:
            temporary_refusal = automatic_refusal(0)
            if s.persistent_cache:
                root_refusal = automatic_refusal(0)
        if s.persistent_cache and root_refusal is None:
            root = s.cache_dir
            if root is None:
                from install import paths  # the fake's own data dir

                root = paths.default_cache_dir()
            # RuntimeResources.mm persistentCacheNamespace: 32 lowercase hex digits
            # (128 bits), which is what the manager's Clear KV cache recognises.
            # fp32 states keep the identity they had before states had a format.
            states = " bf16" if s.state_format == "bf16" else ""
            namespace = hashlib.sha256(f"{s.model}|{s.kv_format}{states}".encode()).hexdigest()[:32]
            cache = PersistentCache(root, namespace)
            if cache.open():
                self.persistent = cache
            else:
                # RuntimeResources.mm:124-126
                log_startup(
                    f"Persistent cache {cache.directory} is in use by another process; "
                    "this one keeps a temporary cache."
                )
        off = None
        if self.persistent is None and temporary_refusal is not None:
            if root_refusal is not None:
                off = f"for its root, {root_refusal}; for temporary files, {temporary_refusal}"
            else:
                off = f"for temporary files, {temporary_refusal}" if s.persistent_cache else temporary_refusal
        elif self.persistent is None and root_refusal is not None:
            log_startup(f"Persistent cache off: {root_refusal}; the SSD cache keeps temporary files.")
        if off is not None:
            # RuntimeResources.mm:655-657
            log_startup(f"SSD cache off: {off}; the state staging the plan set aside stays unused.")
            return
        quota = s.cache_disk_quota
        held = self.persistent.taken_back["bytes"] if self.persistent else 0
        self.disk_open = True
        self.disk_limit = min(quota, room(held) // 4)
        if self.disk_limit < quota:
            reason = f"a quarter of the {gibibytes(room(held))} its volume has"
        else:
            reason = f"{'its automatic quota' if automatic else 'its quota'}, while its volume has room"
        if self.persistent:
            files = f"persistent files in {self.persistent.directory}"
        else:
            files = f"temporary files in {tempfile.gettempdir()}"
        budget = ""
        if s.max_cache_disk_writes:
            budget = (
                f"; a write budget of {gibibytes(s.max_cache_disk_writes)} a day, which the states conversations "
                "resume from and KV pages pass, starting with a quarter of a day's"
            )
        slot_kib = plan.page_bytes // KIB + 4
        state_mib = state_bytes // MIB
        # RuntimeResources.mm reportDiskTier (lines 149-184)
        log_startup(
            f"SSD cache: up to {gibibytes(self.disk_limit)} of {files} ({reason} beyond the "
            f"{gibibytes(floor)} it keeps free){budget}; KV pages of {slot_kib} KiB and states of {state_mib} MiB, "
            f"a state's write staging through {state_mib} MiB of the memory plan; --max-cache-disk 0 turns it off."
        )
        if self.persistent:
            t = self.persistent.taken_back
            # RuntimeResources.mm:768-773
            log_startup(
                f"Persistent cache {self.persistent.directory}: took back {t['states']} restore points over "
                f"{t['kv_blocks']} KV blocks ({t['bytes'] // MIB} MiB); left {t['left_behind']} copies behind."
                + (
                    " The last process did not stop cleanly: this one serves on probation for its first minute."
                    if self.persistent.unclean
                    else ""
                )
            )

    # --- modes ---------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise APIError(400, f"mode must be one of {', '.join(MODES)}")
        with self.lock:
            previous, self.mode = self.mode, mode
            if mode == "engine_recovering":
                self.engine_error = "native protocol reached EOF"
                self.fatal_error = None
            elif mode == "engine_failed":
                self.engine_error = self.fatal_error = (
                    "the inference engine failed 3 times in a row, each within 60 s of starting "
                    "(native protocol reached EOF); Splash stopped restarting it. Set "
                    "SPLASH_CRASH_TRACE=1 to record a crash trace. Restart the Splash server "
                    "after fixing the cause."
                )
            else:
                self.fatal_error = None
        # server/backend.py _failed / _cache_status
        if mode in ("engine_recovering", "engine_failed") and previous not in ("engine_recovering", "engine_failed"):
            print_status("Engine failed · native protocol reached EOF", error=True)
        if mode == "engine_failed":
            print_status(f"Engine stopped · {self.fatal_error}", error=True)
        if previous == "engine_recovering" and mode not in ("engine_recovering", "engine_failed"):
            with self.lock:
                self.restarts += 1
                self.engine_error = None
            print_status("Engine restarted")

    @property
    def transport_ready(self) -> bool:
        return self.ready and not self.closing and self.mode not in ("engine_recovering", "engine_failed")

    def refusal(self) -> APIError | None:
        """server/backend.py NativeBackend.refusal."""
        with self.lock:
            if self.closing:
                return APIError(503, "server is shutting down", "server_shutdown")
            if self.mode == "engine_failed":
                return APIError(500, self.fatal_error or "engine failed", "engine_failed")
            if self.mode == "engine_recovering" or not self.ready:
                failure = self.engine_error
                return APIError(
                    503,
                    "engine is recovering; retry shortly" + (f" (last failure: {failure})" if failure else ""),
                    "engine_recovering",
                )
        return None

    def queue_full(self) -> bool:
        return self.mode == "queue_full"

    # --- prefix cache ----------------------------------------------------------
    @staticmethod
    def _salted(tokens: list[int], salt: str | None) -> tuple[int, ...]:
        """The cache key of a prompt: a request shares cached prompts only with
        requests of the same cache_salt (frontend.py _cache_salt, 1.3.2). A
        salt leads the key as a token no vocabulary has."""
        return tuple(tokens) if salt is None else (-1 - zlib.crc32(salt.encode()), *tokens)

    def _lookup(self, tokens: list[int], salt: str | None = None) -> int:
        best = 0
        key = self._salted(tokens, salt)
        with self.lock:
            for cached in self.prefix_cache:
                limit = min(len(cached), len(key))
                n = 0
                while n < limit and cached[n] == key[n]:
                    n += 1
                best = max(best, n)
        best = min(best - (salt is not None), len(tokens) - 1)
        return max(0, best // KV_PAGE_TOKENS * KV_PAGE_TOKENS)

    def _publish(self, tokens: list[int], salt: str | None = None) -> None:
        with self.lock:
            self.prefix_cache[self._salted(tokens, salt)] = None
            self.c["publications"] += 1
            while len(self.prefix_cache) > 32:
                evicted, _ = self.prefix_cache.popitem(last=False)
                self.c["evictions"] += 1
                if self.disk_open:
                    self.c["kv_demotions"] += 1
                    self.c["disk_written_bytes"] += len(evicted) * self._page_bytes() // KV_PAGE_TOKENS
                    self.c["durable"] += 1

    def _page_bytes(self) -> int:
        return self.plan.page_bytes if self.plan else 32 * KIB * KV_PAGE_TOKENS

    # --- weights keep-alive ----------------------------------------------------
    @property
    def idle_release_seconds(self) -> float:
        """--idle-release, else FAKE_SPLASH_IDLE_RELEASE_SECONDS (default 600)."""
        given = self.settings.idle_release
        return self.config.idle_release_seconds if given is None else given

    def _release_weights(self) -> bool:
        """NativeRuntime.cpp releaseWeights: only while no request is in
        service and no restore is under way (caller holds the lock)."""
        if not self.ready or self.weights_released or self.weights_restoring or self.active:
            return False
        self.weights_released = True
        return True

    def _log_release(self, message: str) -> None:
        if self.settings.ane_split:
            write_stderr_line("Neural Engine FFN program unloaded while idle")  # ops/AneFfn.cpp:631
        write_stderr_line(message)

    def idle_tick(self) -> None:
        with self.lock:
            idle = time.monotonic() - self.last_request
            release = idle >= self.idle_release_seconds and self._release_weights()
        if release:
            # runtime/engine/NativeRuntime.cpp:133
            self._log_release(
                f"Weights released after {self.idle_release_seconds:g} s without a request; "
                "the next request restores them"
            )

    def release_on_request(self) -> dict:
        """POST /v1/memory/release (1.3.2, NativeRuntime.cpp handleRelease): the
        weights go back now when the engine is idle, with --idle-release off
        too; the answer is the weights section either way."""
        with self.lock:
            released = self._release_weights()
        if released:
            # NativeRuntime.cpp:373
            self._log_release("Weights released on request; the next request restores them")
        return self.weights_status()

    def weights_status(self) -> dict:
        """runtime/engine/Status.cpp appendWeights: null while --idle-release off."""
        with self.lock:
            return {
                "idle_release_seconds": None if math.isinf(self.idle_release_seconds) else self.idle_release_seconds,
                "released": self.weights_released,
                "restoring": self.weights_restoring,
                "restores": self.weights_restores,
                "restore_failures": 0,
            }

    def _restore_weights(self) -> Iterator[tuple]:
        with self.lock:
            restore = self.weights_released and not self.weights_restoring
            if restore:
                self.weights_restoring = True
        if not restore:
            return
        started = time.monotonic()
        try:
            yield from self._sleep(self.config.restore_seconds)
        finally:
            with self.lock:
                self.weights_restoring = False
        # runtime/engine/NativeRuntime.cpp:160
        write_stderr_line(f"Weights restored in {time.monotonic() - started:.2f} s")
        with self.lock:
            self.weights_released = False
            self.weights_restores += 1
        if self.settings.ane_split:
            # ops/AneFfn.cpp:643
            write_stderr_line(f"Neural Engine FFN program reloaded in {self.config.restore_seconds / 3:.2f} s")

    # --- generation --------------------------------------------------------------
    def _sleep(self, seconds: float, job: FakeJob | None = None) -> Iterator[tuple]:
        """Sleep, yielding ("idle",) every keepalive interval."""
        end = time.monotonic() + max(0.0, seconds)
        keepalive = max(0.05, self.config.keepalive_seconds)
        last = time.monotonic()
        while True:
            now = time.monotonic()
            if self.closing:
                raise APIError(503, "server is shutting down", "server_shutdown")
            if job is not None and now >= job.deadline:
                raise APIError(504, "request timed out", "request_timeout")
            if now >= end:
                return
            time.sleep(min(end - now, 0.05))
            if time.monotonic() - last >= keepalive:
                last = time.monotonic()
                yield ("idle",)

    def _acquire_lane(self, job: FakeJob) -> Iterator[tuple]:
        keepalive = max(0.05, self.config.keepalive_seconds)
        with self.lock:
            self.c["queued"] += 1
        waited = time.monotonic()
        try:
            while not self.lanes.acquire(timeout=0.05):
                if self.closing:
                    raise APIError(503, "server is shutting down", "server_shutdown")
                if time.monotonic() >= job.deadline:
                    raise APIError(504, "request timed out", "request_timeout")
                if time.monotonic() - waited >= keepalive:
                    waited = time.monotonic()
                    yield ("idle",)
        finally:
            with self.lock:
                self.c["queued"] -= 1

    def _admit(self, job: FakeJob) -> int:
        """Track a request in service for /status active_requests (Status.cpp,
        1.3.2): its phase, prompt progress and output so far."""
        with self.lock:
            self.request_ids += 1
            self.active[self.request_ids] = {
                "phase": "queued",
                "priority": job.priority,
                "prompt_tokens": len(job.prompt_tokens),
                "prompt_processed": 0,
                "generated_tokens": 0,
                "max_new_tokens": job.max_new_tokens,
                "received": time.monotonic(),
            }
            return self.request_ids

    def _progress(self, request_id: int, **fields: object) -> None:
        with self.lock:
            if request_id in self.active:
                self.active[request_id].update(fields)

    def _output_plan(self, job: FakeJob) -> tuple[list[str], list[str], list[tuple[str, str]]]:
        """The reasoning pieces, content pieces and tool calls (name, args)."""
        cfg = self.config
        thinking = (job.thinking and cfg.reasoning != "never") or cfg.reasoning == "always"
        reasoning = fake_text.reasoning_pieces(job.last_user) if thinking else []
        if job.thinking_token_budget is not None:
            # A thinking budget (1.3.2) closes the thinking after that many tokens.
            reasoning = reasoning[: job.thinking_token_budget]
        tools = job.tools or []
        choice = job.tool_choice
        chosen: list[dict] = []
        if tools and choice != "none":
            if isinstance(choice, dict):
                wanted = (choice.get("function") or {}).get("name") or choice.get("name")
                chosen = [t for t in tools if fake_text.tool_name(t) == wanted][:1]
            elif choice in ("required", "any"):
                chosen = tools[:1]
            else:
                lowered = job.last_user.lower()
                chosen = [t for t in tools if fake_text.tool_name(t).lower() in lowered][:1]
        if chosen:
            calls = [(fake_text.tool_name(t), fake_text.tool_arguments(t, job.last_user)) for t in chosen]
            return reasoning, [], calls
        if job.response_format and job.response_format.get("type") in ("json_object", "json_schema"):
            schema = (job.response_format.get("json_schema") or {}).get("schema") or {
                "required": ["answer"],
                "properties": {"answer": {"type": "string"}},
            }
            return reasoning, fake_text.pieces(fake_text.tool_arguments({"parameters": schema}, job.last_user)), []
        content = fake_text.reply_pieces(job.last_user or job.prompt_text, cfg.text, cfg.reply_tokens)
        return reasoning, content, []

    def generate(self, job: FakeJob) -> Generator[tuple, None, None]:
        """Events: ("idle",), ("start", matched), ("progress", dict),
        ("text", field, text), ("tool", call_id, name, [argument parts]),
        ("done", NativeResult). Raises APIError for failures."""
        received = time.monotonic()
        with self.lock:
            self.c["submitted"] += 1
            self.last_request = received
        request_id = self._admit(job)
        outcome = "failed"
        holding_lane = counted_tokens = False
        matched = 0
        produced: list[str] = []
        result: NativeResult | None = None
        error_code = "runtime_error"
        started = received
        first_token_at = None
        reason = "stop"
        stop_sequence = None
        try:
            yield from self._acquire_lane(job)
            holding_lane = True
            yield from self._restore_weights()
            if self.mode == "capacity_exhausted":
                with self.lock:
                    self.c["capacity_failures"] += 1
                # server/backend.py:817-824 wrapping Engine.cpp capacityExhausted
                raise APIError(
                    400,
                    "the request does not fit in the memory this server may use, even after every "
                    "cached prefix was evicted; restart the server with a larger --max-memory or a "
                    "smaller --max-context (KV target needs more pages than the memory budget holds)",
                    "capacity_exhausted",
                )
            if self.mode == "resource_timeout":
                with self.lock:
                    self.c["resource_suspensions"] += 1
                # runtime/engine/Engine.cpp:223, retryable -> 503 (backend.py:837)
                raise APIError(
                    503, "memory did not become available within the resource wait limit", "resource_timeout"
                )
            prompt = job.prompt_tokens
            matched = self._lookup(prompt, job.cache_salt)
            uncached = len(prompt) - matched
            with self.lock:
                self.c["prefilling"] += 1
                self.c["cache_hits" if matched else "cold_misses"] += 1
                self.c["kv_hit_tokens"] += matched
                self.c["reused_tokens"] += matched
                self.c["active_tokens"] += len(prompt)
            counted_tokens = True
            started = time.monotonic()
            self._progress(request_id, phase="prefill", prompt_processed=matched)
            self._print_started(job, matched)
            yield ("start", matched)
            prefill_seconds = uncached / max(1.0, self.config.prefill_tokens_per_second)
            steps = max(1, math.ceil(uncached / PREFILL_TOKEN_BUDGET)) if job.return_progress else 1
            try:
                for step in range(1, steps + 1):
                    yield from self._sleep(prefill_seconds / steps, job)
                    processed = matched + min(uncached, step * PREFILL_TOKEN_BUDGET)
                    self._progress(request_id, prompt_processed=processed)
                    if job.return_progress:
                        yield (
                            "progress",
                            {
                                "total": len(prompt),
                                "cache": matched,
                                "processed": processed,
                                "time_ms": (time.monotonic() - started) * 1000.0,
                            },
                        )
            finally:
                with self.lock:
                    self.c["prefilling"] -= 1
                    self.c["prefill_batches"] += steps
                    self.c["prefill_rows"] += uncached
                    self.c["prefill_input_tokens"] += uncached
                    self.c["prefill_wall_ms"] += (time.monotonic() - started) * 1000.0
                    self._count_ane(uncached, prefill_seconds)
            reasoning, content, calls = self._output_plan(job)
            content_text = ""
            self._progress(request_id, phase="decode")
            interval = 1.0 / max(1.0, self.config.tokens_per_second)
            with self.lock:
                self.c["decoding"] += 1
            decode_started = time.monotonic()
            try:
                queue: list[tuple] = [("reasoning_content", p) for p in reasoning]
                queue += [("content", p) for p in content]
                for index, (name, args) in enumerate(calls):
                    parts = ["{", args[1:-1], "}"] if len(args) > 2 else [args]
                    queue.append(("tool", f"call_{job.public_id}_{index}", name, [p for p in parts if p]))
                if job.ignore_eos:
                    filler = fake_text.reply_pieces("", None, job.max_new_tokens)
                    queue += [("content", p) for p in filler]
                budget = job.max_new_tokens
                midstream_at = max(1, len(queue) // 2)
                for position, item in enumerate(queue):
                    if self.mode == "fail_midstream" and position >= midstream_at:
                        # server/backend.py:782-789
                        raise APIError(
                            503,
                            "the inference engine stopped unexpectedly and is restarting; retry the request",
                            "runtime_unavailable",
                        )
                    cost = 1 if item[0] != "tool" else max(1, len(fake_text.pieces(item[2] + "".join(item[3]))))
                    if len(produced) + cost > budget:
                        reason = "length"
                        break
                    yield from self._sleep(interval * cost, job)
                    now = time.monotonic()
                    if first_token_at is None:
                        first_token_at = now
                        self.latencies.observe("http_ttft", now - received)
                        with self.lock:
                            self.ttft_samples.append((now - received) * 1000.0)
                    else:
                        self.latencies.observe("output_interval", interval * cost)
                        with self.lock:
                            self.itl_samples.append(interval * cost * 1000.0)
                    produced.extend(["t"] * cost)
                    self._count_decode(cost)
                    self._progress(request_id, generated_tokens=len(produced))
                    if item[0] == "tool":
                        yield item
                        continue
                    field_name, piece = item
                    if field_name == "content" and job.stop_sequences:
                        combined = content_text + piece
                        hits = [(combined.find(s), s) for s in job.stop_sequences if s in combined]
                        if hits:
                            offset, stop_sequence = min(hits)
                            if offset > len(content_text):
                                yield ("text", "content", combined[len(content_text) : offset])
                            break
                        content_text = combined
                    if field_name == "reasoning_content":
                        job.reasoning_tokens += 1
                    yield ("text", field_name, piece)
            finally:
                with self.lock:
                    self.c["decoding"] -= 1
                    self.c["decode_wall_ms"] += (time.monotonic() - decode_started) * 1000.0
            self._publish(prompt + self._output_ids(job, len(produced)), job.cache_salt)
            result = self._result(job, reason, matched, len(produced), received, started, first_token_at)
            result.stop_sequence = stop_sequence
            outcome = "completed"
            yield ("done", result)
        except GeneratorExit:
            outcome = "cancelled"
            raise
        except RequestCancelled:
            outcome = "cancelled"
            raise
        except APIError as error:
            error_code = error.code
            raise
        finally:
            with self.lock:
                self.c[outcome] += 1
                if counted_tokens:
                    self.c["active_tokens"] -= len(job.prompt_tokens)
                self.last_request = time.monotonic()
                self.active.pop(request_id, None)
            if holding_lane:
                self.lanes.release()
            if outcome == "cancelled":
                result = self._result(job, "cancelled", matched, len(produced), received, started, first_token_at)
            self._print_request(job, result, error_code)

    @staticmethod
    def _output_ids(job: FakeJob, count: int) -> list[int]:
        return [zlib.crc32(f"{job.public_id}{i}".encode()) % fake_text.VOCAB_SIZE for i in range(count)]

    def _result(
        self,
        job: FakeJob,
        reason: str,
        matched: int,
        completion: int,
        received: float,
        started: float,
        first_token_at: float | None,
    ) -> NativeResult:
        """The request's NativeResult (server/backend.py). Its speculative
        decoding is invented but consistent: each verified draft cycle yields
        the tokens it accepted and one more, about 2.4 a cycle."""
        done = time.monotonic()
        first = first_token_at or done
        cycles = math.ceil(completion / 2.4)
        accepted = completion - cycles
        with self.lock:
            self.c["drafted_tokens"] += cycles * 4
            self.c["accepted_draft_tokens"] += accepted
        return NativeResult(
            reason=reason,
            prompt_tokens=len(job.prompt_tokens),
            completion_tokens=completion,
            start_to_first_token_ms=(first - started) * 1000.0,
            first_token_to_done_ms=(done - first) * 1000.0,
            request_wall_ms=(done - received) * 1000.0,
            prefill_tokens=len(job.prompt_tokens) - matched,
            cache=CacheInfo("hit" if matched else "miss", matched, 0),
            first_token_batch_tokens=1 if completion else 0,
            drafted_tokens=cycles * 4,
            accepted_draft_tokens=accepted,
            draft_cycles=cycles,
        )

    @staticmethod
    def _print_started(job: FakeJob, matched: int) -> None:
        """server/backend.py _on_event (1.3.2, lines 823-848): the record the
        engine's admission logs, printed by the copied print_request."""
        record: dict = {"outcome": "started", "prompt_tokens": len(job.prompt_tokens), "cached_tokens": matched}
        if job.reasoning is not None:
            record["reasoning"] = job.reasoning
        print_request(record)

    @staticmethod
    def _print_request(job: FakeJob, result: NativeResult | None, error_code: str) -> None:
        """server/backend.py _record, printed by the copied print_request for
        every generation request."""
        record: dict = {"outcome": result.reason if result else "error", "prompt_tokens": len(job.prompt_tokens)}
        if result is not None:
            record["completion_tokens"] = result.completion_tokens
            record["metrics"] = result.metrics
        if job.tools:
            # frontend.py: sha1 of the sorted-key JSON of the tools, 8 hex digits.
            digest = hashlib.sha1(
                json.dumps(job.tools, sort_keys=True, separators=(",", ":")).encode(), usedforsecurity=False
            ).hexdigest()[:8]
            record["tools"] = {"count": len(job.tools), "signature": digest}
        if result is None:
            record["error_code"] = error_code
        print_request(record)

    def _count_ane(self, rows: int, seconds: float) -> None:
        """A prefill runs in chunks of PREFILL_TOKEN_BUDGET rows; each chunk of
        ANE_MINIMUM_ROWS rows or more runs split, through every layer (caller
        holds the lock)."""
        if not self.settings.ane_split:
            return
        full, rest = divmod(rows, PREFILL_TOKEN_BUDGET)
        split = full + (rest >= ANE_MINIMUM_ROWS)
        if not split:
            return
        self.ane["split_commands"] += split
        self.ane["evaluations"] += split * ANE_LAYERS
        self.ane["ane_ms"] += seconds * 1000.0 * ANE_SHARE

    def _count_decode(self, tokens: int) -> None:
        with self.lock:
            width = min(MAX_BATCH_WIDTH, max(1, self.c["decoding"]))
            self.c[f"b{width}"] += 1
            self.c["decode_batches"] += 1
            self.c["decode_output_tokens"] += tokens

    def score(self, job: FakeJob, options: int) -> Generator[tuple, None, None]:
        """A score-only job (judgments, System One): prefill, then option
        logits derived from the prompt."""
        received = time.monotonic()
        with self.lock:
            self.c["submitted"] += 1
        # Engine.cpp snapshot: a score-only job is a request in service too.
        request_id = self._admit(job)
        outcome = "failed"
        holding_lane = False
        try:
            yield from self._acquire_lane(job)
            holding_lane = True
            yield from self._restore_weights()
            matched = self._lookup(job.prompt_tokens, job.cache_salt)
            self._progress(request_id, phase="prefill", prompt_processed=matched)
            started = time.monotonic()
            yield ("start", matched)
            yield from self._sleep(
                (len(job.prompt_tokens) - matched) / max(1.0, self.config.prefill_tokens_per_second), job
            )
            seed = zlib.crc32(job.prompt_text.encode())
            logits = tuple(float(((seed >> (i % 24)) % 97) / 10.0 - 4.0 + i * 0.01) for i in range(options))
            done = time.monotonic()
            self._publish(job.prompt_tokens, job.cache_salt)
            with self.lock:
                self.c["prefill_input_tokens"] += len(job.prompt_tokens) - matched
            outcome = "completed"
            yield (
                "done",
                NativeResult(
                    "stop",
                    len(job.prompt_tokens),
                    0,
                    (done - started) * 1000.0,
                    0.0,
                    (done - received) * 1000.0,
                    len(job.prompt_tokens) - matched,
                    CacheInfo("hit" if matched else "miss", matched, 0),
                    option_logits=logits,
                ),
            )
        except GeneratorExit:
            outcome = "cancelled"
            raise
        finally:
            with self.lock:
                self.c[outcome] += 1
                self.active.pop(request_id, None)
            if holding_lane:
                self.lanes.release()

    # --- shutdown --------------------------------------------------------------
    def close_persistent(self, flushed: bool) -> None:
        if self.persistent is None or not flushed:
            return
        with self.lock:
            states = self.c["publications"] + self.persistent.taken_back["states"]
            blocks = sum(len(k) for k in self.prefix_cache) // KV_PAGE_TOKENS + self.persistent.taken_back["kv_blocks"]
        self.persistent.close(states, blocks, blocks * self._page_bytes())

    # --- /status ---------------------------------------------------------------
    def status(self, server_info: dict) -> dict:
        s, cfg = self.settings, self.config
        plan = self.plan
        b = plan.breakdown if plan else None
        with self.lock:
            c = dict(self.c)
            ane = dict(self.ane)
            cache_tokens = sum(len(k) for k in self.prefix_cache)
            ttft = deque(self.ttft_samples)
            itl = deque(self.itl_samples)
            now = time.monotonic()
            # Status.cpp (1.3.2) active_requests, oldest first.
            active_requests = [
                {
                    "id": request_id,
                    "phase": request["phase"],
                    "priority": request["priority"],
                    "prompt_tokens": request["prompt_tokens"],
                    "prompt_processed": request["prompt_processed"],
                    "generated_tokens": request["generated_tokens"],
                    "max_new_tokens": request["max_new_tokens"],
                    "age_ms": int((now - request["received"]) * 1000),
                }
                for request_id, request in sorted(self.active.items())
            ]
        page = self._page_bytes()
        pages_cache = cache_tokens // KV_PAGE_TOKENS
        pages_active = math.ceil(c["active_tokens"] / KV_PAGE_TOKENS)
        extent_pages = b.kv_extent_pages if b else 64
        pages_allocated = math.ceil(max(1, pages_cache + pages_active) / extent_pages) * extent_pages
        kv_bytes = pages_allocated * page
        weights = 0
        if b and not self.weights_released:
            weights = b.target_weights_bytes + b.draft_weights_bytes + b.vision_weights_bytes
        fixed_rest = (b.shared_prefill_bytes + b.shared_decode_bytes) if b else 0
        state_bytes = (b.lane_state_bytes if b else 0) * min(MAX_BATCH_WIDTH, c["prefilling"] + c["decoding"])
        current = weights + fixed_rest + kv_bytes + state_bytes
        with self.lock:
            self.peak_bytes = max(self.peak_bytes, current)
            peak = self.peak_bytes
        limit = b.hard_budget_bytes if b else 0
        pressure = cfg.memory_pressure if cfg.memory_pressure in PRESSURES else "normal"
        metal_ok = True
        ready = self.ready and metal_ok and pressure != "critical" and (b is None or current <= limit)
        hits, misses = c["cache_hits"], c["cold_misses"]
        drafted = c["drafted_tokens"]
        decode_ms = c["decode_wall_ms"]
        prefill_ms = c["prefill_wall_ms"]
        persistent = self.persistent is not None
        taken_back = (
            self.persistent.taken_back
            if self.persistent
            else {"states": 0, "kv_blocks": 0, "bytes": 0, "left_behind": 0}
        )
        quota = s.cache_disk_quota if self.disk_open else 0
        disk_used = min(self.disk_limit, c["disk_written_bytes"] + taken_back["bytes"]) if self.disk_open else 0
        # SlotFile.hpp writeBudgetBytes: a budget starts with a quarter of a
        # day's writes; zero without one.
        write_budget = max(0, s.max_cache_disk_writes // 4 - c["disk_written_bytes"]) if self.disk_open else 0
        gated = cfg.writes_gated if cfg.writes_gated in ("no", "budget", "closed") else "no"
        if s.max_cache_disk_writes and self.disk_open and c["disk_written_bytes"] > s.max_cache_disk_writes // 4:
            gated = "budget"
        status: dict = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "ready": ready,
            "maximum_context_tokens": self.max_context,
            "memory_pressure": pressure,
            # Status.cpp (1.3.1 on): ProcessInfo's thermal state; readiness ignores it.
            "thermal_state": cfg.thermal_state,
            "admission": {
                "waiting": c["queued"],
                "waiting_memory": 0,
                "waiting_concurrency": c["queued"],
                "held_behind_refusal": 0,
                "restoring": 0,
                "suspended": 0,
                "draining": False,
                "oldest_wait_ms": 0.0,
            },
            "loop": {"max_tick_ms": round(1000.0 / max(1.0, cfg.tokens_per_second), 3)},
            "identity": {
                "cache": {
                    "loaded_model_layout_sha256": hashlib.sha256(f"layout|{s.model}".encode()).hexdigest(),
                    "build_id": self.build_id,
                    "dtype": "int8" if s.kv_format == "int8" else "bf16",
                    "block_tokens": KV_PAGE_TOKENS,
                },
                "kv": {
                    "target_model_sha256": hashlib.sha256(f"target|{s.model}".encode()).hexdigest(),
                    "format": s.kv_format,
                    "quantization": "symmetric_int8" if s.kv_format == "int8" else "none",
                    "scale_type": "float32" if s.kv_format == "int8" else "none",
                    "key_layout": "token_major",
                    "value_layout": "dimension_major",
                },
            },
            "memory_plan": plan.status_json() if plan else {"valid": False},
            "memory_actual": {"allocated_bytes": current, "current_bytes": current, "peak_bytes": peak},
            "memory_governor": {
                "limit_bytes": limit,
                "charged_bytes": current,
                "headroom_bytes": max(0, limit - current),
                "growth_allowed": pressure == "normal",
                "denied_reservations": c["denied_reservations"],
                "system_pressure": pressure,
                "host_measurement_valid": True,
                "host_available_bytes": cfg.host_available,
                "host_reserve_bytes": 4 * GIB,
                # 1.3.2: the spare memory macOS has beyond what it can reclaim,
                # of which serving keeps a reserve.
                "host_spare_bytes": max(0, cfg.host_available - 2 * GIB),
                "host_spare_reserve_bytes": 2 * GIB,
                "host_headroom_bytes": max(0, cfg.host_available - 4 * GIB),
            },
            "memory_audit": {
                "scope": "startup_warmup",
                "valid": True,
                "error": "none",
                "categorized_bytes": weights + fixed_rest,
                "backend_unclassified_bytes": 0,
                "device_untracked_bytes": 0,
                "device_peak_deviation_basis_points": 0,
                "actual_headroom_bytes": max(0, limit - current),
                "backend_allocated_bytes": current,
                "device_current_allocated_bytes": current,
                "device_peak_allocated_bytes": peak,
                "backend_peak_allocated_bytes": peak,
            },
            "weights": self.weights_status(),
            # Status.cpp appendAneFfn
            "ane_ffn": {
                "state": "split" if s.ane_split else "off",
                "share": ANE_SHARE if s.ane_split else 0,
                "minimum_rows": ANE_MINIMUM_ROWS if s.ane_split else 0,
                "reason": ANE_SPLIT_REASON
                if s.ane_split
                else (ANE_OFF_GIVEN if _family(s.model) == "Qwen3.8-27B" else ANE_NOT_DENSE),
                "split_commands": ane["split_commands"],
                "reruns": 0,
                "ane_ms": round(ane["ane_ms"], 6),
                "evaluations": ane["evaluations"],
            },
            "kv": {
                "block_tokens": KV_PAGE_TOKENS,
                "pages_allocated": pages_allocated,
                "pages_active": pages_active,
                "pages_cache": pages_cache,
                "pages_free": max(0, pages_allocated - pages_active - pages_cache),
                "allocated_bytes": kv_bytes,
                "reclaimable_bytes": pages_cache * page,
                "extent_allocations": pages_allocated // extent_pages,
                "extent_releases": 0,
                "extent_allocate_max_ms": 0.4,
                "extent_release_max_ms": 0.0,
                "extent_compactions": 0,
                "pages_moved": 0,
                "extent_compact_max_ms": 0.0,
            },
            "state": {
                "entries": len(self.prefix_cache),
                "pinned": 0,
                "in_use": c["prefilling"] + c["decoding"],
                "in_use_evictions": 0,
                "bytes": len(self.prefix_cache) * (b.lane_state_bytes if b else 0),
                "allocated_bytes": state_bytes,
                "active_lanes": c["prefilling"] + c["decoding"],
                "idle_gdn_cells": 0,
                "idle_draft_rings": 0,
                "idle_context_windows": 0,
                "publications": c["publications"],
                "evictions": c["evictions"],
                "checkpoint_entries": 0,
                "checkpoint_bytes": 0,
                "checkpoint_evictions": 0,
                "checkpoint_retirements": 0,
                "disk_hits": c["kv_restores"],
                "disk_promotions": c["kv_restores"],
                "disk_promotions_skipped": 0,
                "disk_bytes": disk_used,
                "offloads": c["kv_demotions"],
                "offload_failures": 0,
                "invalidations": 0,
            },
            "disk": {
                "capacity_bytes": quota,
                "limit_bytes": self.disk_limit if self.disk_open else 0,
                "used_bytes": disk_used,
                "file_bytes": disk_used,
                "volume_available_bytes": cfg.volume_available if self.disk_open else 0,
                "writes_gated": gated,
                "write_budget_bytes": write_budget,
                "read_bytes": c["disk_read_bytes"],
                "written_bytes": c["disk_written_bytes"],
                "kv_blocks": disk_used // page if page else 0,
                "kv_bytes": disk_used,
                "kv_demotions": c["kv_demotions"],
                "kv_demotion_failures": 0,
                "kv_demotions_refused": 0,
                "kv_restores": c["kv_restores"],
                "kv_restore_failures": 0,
                "kv_pending_pages": 0,
                "persistent": persistent,
                "kv_copies": c["durable"],
                "kv_copy_failures": 0,
                "taken_back": dict(taken_back),
                "write_behind": {"waiting": 0, "durable": c["durable"], "unneeded": 0, "refused": 0},
            },
            "cache": {
                "probe_hashed_blocks": c["kv_hit_tokens"] // KV_PAGE_TOKENS,
                "hits": hits,
                "cold_misses": misses,
                "hit_rate": hits / (hits + misses) if hits + misses else 0.0,
                "kv_hit_tokens": c["kv_hit_tokens"],
                "kv_disk_hit_tokens": c["kv_restores"] * KV_PAGE_TOKENS,
                "lost_state_misses": 0,
                "reused_tokens": c["reused_tokens"],
                "deduplicated_kv_tokens": 0,
                "replay_state_publications": 0,
                "deduplicated_state_publications": 0,
                "recycled_state_publications": 0,
                "disk_state_publications": c["durable"],
                "state_captures": c["publications"],
                "capture_cuts": 0,
                "replay_state_publication_failures": 0,
                "lazy_junctions": 0,
                "junction_materializations": 0,
                "junction_materialization_failures": 0,
                "checkpoint_publications": 0,
                "checkpoint_publication_failures": 0,
                "resource_suspensions": c["resource_suspensions"],
                "priority_suspensions": 0,
                "resource_resumptions": 0,
                "resource_replay_tokens": 0,
            },
            "draft_context": {
                "target_prefill_rows": c["prefill_rows"],
                "prompt_end_rows": c["prefill_batches"],
                "materialization_rows": 0,
                "avoided_rows": 0,
                "restore_skipped": 0,
                "resets": 0,
            },
            "model_timing": {
                "scope": "model_lifetime",
                "prefill": {
                    "last_gpu_ms": 0.0,
                    "last_wall_ms": 0.0,
                    "total_gpu_ms": prefill_ms * 0.9,
                    "total_wall_ms": prefill_ms,
                },
                "decode": {
                    "last_gpu_ms": 0.0,
                    "last_wall_ms": 0.0,
                    "total_gpu_ms": decode_ms * 0.9,
                    "total_wall_ms": decode_ms,
                },
            },
            "constraint_masks": {
                "overlap_batches": 0,
                "overlap_requests": 0,
                "last_target_forward_gpu_ms": 0.0,
                "total_target_forward_gpu_ms": 0.0,
                "last_residual_wait_ms": 0.0,
                "total_residual_wait_ms": 0.0,
            },
            "images": {
                "encodes": c["image_encodes"],
                "embedding_reuses": 0,
                "arena_bytes": (64 * MIB if self.vision else 0),
                "cached_bytes": 0,
                "state_held_bytes": 0,
                "rows_bytes": 0,
            },
            "scheduler": {
                "queued": c["queued"],
                "waiting_resources": 0,
                "waiting_prefix": 0,
                "prefilling": c["prefilling"],
                "decoding": c["decoding"],
                "waiting_mask": 0,
                "terminal": 0,
                "prefill_batches": c["prefill_batches"],
                "prefill_rows": c["prefill_rows"],
                "decode_batches": c["decode_batches"],
                "decode_batches_by_width": {k: c[k] for k in ("b1", "b2", "b3", "b4")},
            },
            "active_requests": active_requests,
            "requests": {k: c[k] for k in ("submitted", "completed", "cancelled", "failed")},
            "metrics": {
                "ttft_ms": {"p50": _percentile(ttft, 0.5), "p95": _percentile(ttft, 0.95), "samples": len(ttft)},
                "itl_ms": {"p50": _percentile(itl, 0.5), "p95": _percentile(itl, 0.95), "samples": len(itl)},
                "prefill_input_tokens": c["prefill_input_tokens"],
                "prefill_wall_ms": prefill_ms,
                "prefill_tokens_per_second": c["prefill_input_tokens"] * 1000.0 / prefill_ms if prefill_ms else 0.0,
                "decode_output_tokens": c["decode_output_tokens"],
                "decode_wall_ms": decode_ms,
                "decode_cycle_ms": decode_ms,
                "decode_tokens_per_second": c["decode_output_tokens"] * 1000.0 / decode_ms if decode_ms else 0.0,
                "drafted_tokens": drafted,
                "accepted_draft_tokens": c["accepted_draft_tokens"],
                "draft_acceptance_rate": c["accepted_draft_tokens"] / drafted if drafted else 0.0,
                "capacity_failures": c["capacity_failures"],
                "metal_failures": 0,
                "current_prefill_batch": _batch(c["prefilling"], cfg.prefill_tokens_per_second),
                "current_decode_batch": _batch(c["decoding"], cfg.tokens_per_second),
            },
            "warmup": {
                f"prefill_{PREFILL_TOKEN_BUDGET}": True,
                "decode_b1": True,
                "decode_b2": True,
                "decode_b3": True,
                "decode_b4": True,
                "composite_state_restore": True,
                "memory_limited_steps": [],
                "detail": "",
            },
            "metal": {"healthy": metal_ok, "failure_reason": ""},
        }
        # server/backend.py NativeBackend.status
        transport_ready = self.transport_ready
        stopped = self.mode == "engine_failed"
        status["transport"] = {
            "ready": transport_ready,
            "recovering": not self.closing and not stopped and not transport_ready,
            "stopped": stopped,
            "pending": c["queued"] + c["prefilling"] + c["decoding"],
            "pending_limit": s.queue_size,
            "restarts": self.restarts,
            "last_crash_trace": None,
            "status_stale": not transport_ready,
            "status_age_ms": 0.0,
        }
        if not transport_ready:
            status["transport"]["error"] = self.engine_error or "native process is not ready"
            status["ready"] = False
        # server/frontend.py Frontend.status
        status["vision"] = self.vision
        status["input_modalities"] = self.input_modalities
        later = {"native": "native", "patched": "patched", "unsupported": "unsupported"}.get(cfg.template, "patched")
        status["chat_template"] = {"later_system": later}
        status["frontend"] = {"preparation_capacity": MAX_BATCH_WIDTH, "active": 0, "waiting": 0}
        status["grammar_cache"] = {
            "entries": 0,
            "capacity": 64,
            "source_bytes": 0,
            "source_budget_bytes": 16 * MIB,
            "hits": 0,
            "misses": 0,
        }
        status["response_store"] = self.response_store.stats()
        status["image_cache"] = {
            "entries": 0,
            "bytes": 0,
            "budget_bytes": 256 * MIB,
            "request_bytes": 0,
            "request_budget_bytes": 512 * MIB,
        }
        status["tokenizer_cache"] = {
            "enabled": True,
            "entries": self.tokenizer_cache["entries"],
            "bytes": self.tokenizer_cache["bytes"],
            "budget_bytes": 64 * MIB,
            "capacity": 256,
            "hits": self.tokenizer_cache["hits"],
            "reused_tokens": self.tokenizer_cache["reused_tokens"],
        }
        status["latency"] = self.latencies.snapshot()
        # server/server.py FrontendServer.status
        status["instance"] = {
            "id": self.instance_id,
            "pid": os.getpid(),
            "model": s.model,
            "host": server_info.get("host", s.host),
            "port": server_info.get("port", s.port),
            "started_at": self.started_at,
        }
        status["http"] = {
            "requests": self.requests.stats(),
            "request_body_bytes": self.request_bodies.stats(),
            "max_request_bytes": s.max_request_size,
            "token_counts": self.token_counts.stats(),
            "connections": self.connections.stats(),
        }
        return status


def _batch(width: int, rate: float) -> dict:
    """Status.cpp appendBatch."""
    active = width > 0
    return {
        "valid": active,
        "width": min(width, MAX_BATCH_WIDTH),
        "input_tokens": width if active else 0,
        "output_tokens": width * 2 if active else 0,
        "drafted_tokens": width * 4 if active else 0,
        "accepted_draft_tokens": width * 3 if active else 0,
        "wall_ms": 1000.0 / max(1.0, rate) if active else 0.0,
        "tokens_per_second": rate if active else 0.0,
    }

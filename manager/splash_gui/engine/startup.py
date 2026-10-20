"""Engine output lines → structured events.

Every pattern below is a line Splash prints (1.4.0 unless noted); the source is
noted beside each. Matching is lenient (case-insensitive substrings where possible), the
`HH:MM:SS ` prefix Splash's `print_status` and `logStartup` add is optional, and
the raw text is always kept by the caller. Lines the parser does not know
produce no event.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from ..models.compat import explain_refusal
from ..schemas import EngineError, EngineSuggestion, MemoryBudgetRow

_STAMP = re.compile(r"^\d\d:\d\d:\d\d ")
# runtime/engine/MemoryPlan.cpp EngineMemoryBreakdown::describe: "<label>: <N> bytes (<x> MiB)"
_BUDGET_ROW = re.compile(r"^([a-zA-Z][^:]{1,80}):\s+(.*)$")
_BYTES = re.compile(r"(\d+) bytes")
_CONTEXT = re.compile(r"context ([\d,]+K?)")
_MAX_CONTEXT_EXCEEDS = re.compile(
    r"--max-context (\d+) exceeds the (\d+) tokens.*?omit it or pass at most (\d+)"
)
_WEIGHTS_LOADED = re.compile(r"Weights loaded in ([\d.]+) s")
_WEIGHTS_RESTORED = re.compile(r"Weights restored in ([\d.]+) s")
_TAKEN_BACK = re.compile(
    r"took back (\d+) restore points over (\d+) KV blocks \((\d+) MiB\); left (\d+) copies behind"
)
# The streamed stores' settings the memory planner refuses before the weights load
# (runtime/engine/MemoryPlan.cpp resolve and the hot block cache's floor,
# RuntimeResources.mm "--ple-memory cannot stream the PLE table"), printed as
# "runtime bootstrap failed [resource_assembly/configuration]: <option> …".
_STORE_SETTINGS = {
    "--expert-memory": ("serve.expert_memory", "Expert memory"),
    "--ple-memory": ("serve.ple_memory", "PLE table memory"),
    "--sparse-kv-memory": ("serve.sparse_kv_memory", "Sparse KV memory"),
}
_REFUSED_SETTING = re.compile(r"\]:\s*(--(?:expert|ple|sparse-kv)-memory)\b(.*)$")
# Options a start had no use for (RuntimeResources.mm reportIgnoredSetting and the
# KV spill's "--sparse-kv-memory ignored: …").
_IGNORED_SETTING = re.compile(r"^--[a-z-]+ ignored: ")
# server.py holds the model's assembly itself and stops on a ModelError
# (install/assembly.py `hold`).
_UNPREPARED = ("names no model splash prepared", "is not a model directory splash prepared")

Phase = Literal["installing", "loading", "warming"]
TemplateMode = Literal["native", "patched", "unsupported"]
NoticeKind = Literal[
    "disk_cache_off",
    "persistent_cache_off",
    "hub_unreachable",
    "new_commit_not_installed",
    "weights_restored",
    "schema_unrecognized",
    "setting_ignored",
    "yarn_on",
    "persistent_cache_emptied",
    "other",
]


@dataclass(frozen=True)
class PhaseEvent:
    phase: Phase


@dataclass(frozen=True)
class TemplateEvent:
    mode: TemplateMode


@dataclass(frozen=True)
class ReadyEvent:
    model: str | None
    context_tokens: int | None
    language_only: bool


@dataclass(frozen=True)
class ErrorEvent:
    error: EngineError


@dataclass(frozen=True)
class NoticeEvent:
    kind: NoticeKind
    message: str


@dataclass(frozen=True)
class TransportEvent:
    kind: Literal["failed", "restarted", "stopped"]
    message: str


@dataclass(frozen=True)
class WeightsEvent:
    kind: Literal["released", "restored"]
    seconds: float | None = None


@dataclass(frozen=True)
class TakenBackEvent:
    states: int
    kv_blocks: int
    mib: int
    left_behind: int


Event = (
    PhaseEvent
    | TemplateEvent
    | ReadyEvent
    | ErrorEvent
    | NoticeEvent
    | TransportEvent
    | WeightsEvent
    | TakenBackEvent
)


def strip_stamp(line: str) -> str:
    return _STAMP.sub("", line.rstrip("\r\n"), count=1)


def parse_context(text: str) -> int | None:
    """`256K` → 262144, `100,000` → 100000 (server.py's Ready line format)."""
    text = text.strip().replace(",", "")
    try:
        if text.upper().endswith("K"):
            return int(text[:-1]) * 1024
        return int(text)
    except ValueError:
        return None


def template_mode(text: str) -> TemplateMode | None:
    """server/chat_templates.py `_DESCRIPTIONS`."""
    lowered = text.lower()
    if "requests with later system messages are rejected" in lowered:
        return "unsupported"
    if "patched to render later system messages" in lowered:
        return "patched"
    if "renders later system messages in place" in lowered:
        return "native"
    return None


def _suggestions_for_budget(
    context_cap: int | None, *, disk: bool = False
) -> list[EngineSuggestion]:
    """The fixes for a start the budget refused. Where the SSD cache's room bounds a
    block-sparse model's context, more memory and less vision do not help: a larger
    SSD cache (or more free disk space) does."""
    out = []
    if context_cap is not None:
        out.append(
            EngineSuggestion(
                action="lower_max_context",
                label=f"Use a context of {context_cap:,} tokens",
                patch={"serve.max_context": str(context_cap)},
            )
        )
    else:
        out.append(
            EngineSuggestion(
                action="lower_max_context",
                label="Lower the context limit",
                patch={"serve.max_context": "64K"},
            )
        )
    if disk:
        out.append(
            EngineSuggestion(
                action="enable_ssd_cache",
                label="Let the SSD cache follow the disk (auto)",
                patch={"serve.max_cache_disk": "auto"},
            )
        )
        return out
    out += [
        EngineSuggestion(
            action="language_only",
            label="Skip vision (--language-only)",
            patch={"serve.language_only": True},
        ),
        EngineSuggestion(
            action="raise_max_memory",
            label="Raise or remove the memory ceiling",
            patch={"serve.max_memory": "auto"},
        ),
        EngineSuggestion(action="smaller_variant", label="Pick a smaller variant", patch=None),
    ]
    return out


def _store_setting_error(text: str) -> EngineError | None:
    """A streamed store's size the engine refused at startup, as a settings error
    whose fix puts the setting back to auto."""
    refused = _REFUSED_SETTING.search(text)
    if refused is None:
        return None
    key, label = _STORE_SETTINGS[refused.group(1)]
    message = f"{refused.group(1)}{refused.group(2)}".strip()
    return EngineError(
        kind="other",
        code="invalid_setting",
        message=f"Settings · {label} ({key}): {message}",
        raw=[text],
        suggestions=[
            EngineSuggestion(
                action="reset_setting", label=f"Set {label} to auto", patch={key: "auto"}
            ),
            EngineSuggestion(action="open_logs", label="Open logs"),
        ],
    )


def classify_error(text: str) -> EngineError | None:
    """An error line from the launcher, installer or server as an EngineError."""
    lowered = text.lower()
    message = re.sub(r"^(error:\s*|error · )", "", text, flags=re.IGNORECASE).strip()
    if setting := _store_setting_error(text):
        return setting
    if (
        "set hf_token or run 'hf auth login'" in lowered
        or "cannot access gated repo" in lowered
        or "401 client error" in lowered
        or "403 client error" in lowered
    ):
        return EngineError(
            kind="gated",
            code="gated",
            message="This model requires a Hugging Face token",
            raw=[text],
            suggestions=[
                EngineSuggestion(
                    action="add_hf_token", label="Add a Hugging Face token", patch=None
                )
            ],
        )
    if "repository not found" in lowered or "404 client error" in lowered:
        return EngineError(
            kind="unknown_model",
            code="unknown_model",
            message=message,
            raw=[text],
            suggestions=[EngineSuggestion(action="open_logs", label="Open logs")],
        )
    if (
        "no supported model has this architecture" in lowered
        or "which splash no longer loads" in lowered
        or explain_refusal(text)[1] is not None
        or "stores every projection and expert unquantized" in lowered
        or "unsupported gguf" in lowered
        or "input rotation is not one splash runs" in lowered
        or "has no vision tower" in lowered
        or "splash needs apple gpu family" in lowered
    ):
        suggestions = []
        if "language-only" in lowered or "vision tower" in lowered:
            suggestions.append(
                EngineSuggestion(
                    action="language_only",
                    label="Skip vision (--language-only)",
                    patch={"serve.language_only": True},
                )
            )
        if "choose another variant" in lowered or "choose a quantized variant" in lowered:
            suggestions.append(
                EngineSuggestion(action="smaller_variant", label="Pick another variant")
            )
        return EngineError(
            kind="incompatible",
            code="incompatible",
            # The plain line; `raw` keeps the engine's words.
            message=explain_refusal(message)[0] or message,
            raw=[text],
            suggestions=suggestions,
        )
    # server/backend.py thinking_end_tokens: the engine refuses the setting at startup.
    if "--thinking-end-text must end with" in lowered or (
        "--thinking-end-text takes" in lowered and "the most is" in lowered
    ):
        return EngineError(
            kind="other",
            code="invalid_setting",
            message=f"Settings · Thinking end text (serve.thinking_end_text): {message}",
            raw=[text],
            suggestions=[EngineSuggestion(action="open_logs", label="Open logs")],
        )
    if (
        "cannot bind" in lowered
        or "address already in use" in lowered
        or "splash is already serving" in lowered
        or "unable to start http server" in lowered
    ):
        return EngineError(
            kind="port_in_use",
            code="port_in_use",
            message=message,
            raw=[text],
            suggestions=[EngineSuggestion(action="retry", label="Retry")],
        )
    # server.py (1.4.0) holds the assembly before the engine starts.
    if any(phrase in lowered for phrase in _UNPREPARED) or (
        lowered.startswith("error · cannot hold ") and " in " in lowered
    ):
        return EngineError(
            kind="other",
            code="model_not_prepared",
            message="Splash could not open the installed model; repair it on the Models page, "
            "or check that Splash's models folder is writable",
            raw=[text],
            suggestions=[
                EngineSuggestion(action="retry", label="Retry"),
                EngineSuggestion(action="open_logs", label="Open logs"),
            ],
        )
    if "is not installed in" in lowered:
        return EngineError(kind="unknown_model", code="not_installed", message=message, raw=[text])
    if "neither this installation nor the hub cache records a commit" in lowered or (
        "cannot install" in lowered
        and ("connect" in lowered or "network" in lowered or "resolve" in lowered)
    ):
        return EngineError(
            kind="other",
            code="hub_unreachable",
            message=message,
            raw=[text],
            suggestions=[
                EngineSuggestion(action="retry", label="Retry"),
                EngineSuggestion(action="go_offline", label="Start offline", patch=None),
            ],
        )
    return None


@dataclass
class StartupParser:
    """Stateful: a memory budget breakdown spans many untimestamped lines."""

    _budget: EngineError | None = None
    _budget_rows: list[MemoryBudgetRow] = field(default_factory=list)
    _context_cap: int | None = None
    # The SSD cache's room, not memory, bounds the refused context.
    _disk_bound: bool = False
    # A streamed store's size was refused: the budget block the engine prints after
    # it (the same message, then its breakdown) is that refusal, not another one.
    _setting_refused: bool = False

    def feed(self, raw_line: str) -> list[Event]:
        line = strip_stamp(raw_line)
        if not line.strip():
            return self._end_budget()
        if self._setting_refused and (
            "memory budget validation failed" in line.lower()
            or (_BUDGET_ROW.match(line) and not _STAMP.match(raw_line))
        ):
            return []
        if self._budget is not None:
            if line.startswith("memory_plan_json:"):
                self._attach_plan(line)
                return []
            lowered = line.lower()
            if (
                "memory budget validation failed" in lowered
                or "runtime bootstrap failed" in lowered
                or _MAX_CONTEXT_EXCEEDS.search(line)
            ):
                return self._line(line)
            row = _BUDGET_ROW.match(line)
            if row and not _STAMP.match(raw_line) and not line.lower().startswith("error"):
                label, value = row.group(1).strip(), row.group(2).strip()
                found = _BYTES.search(value)
                self._budget_rows.append(
                    MemoryBudgetRow(
                        label=label, bytes=int(found.group(1)) if found else None, text=value
                    )
                )
                return []
            events = self._end_budget()
            return events + self._line(line)
        return self._line(line)

    def finish(self) -> list[Event]:
        """Flush a pending budget block (call when the process exits)."""
        return self._end_budget()

    def _end_budget(self) -> list[Event]:
        if self._budget is None:
            return []
        error = self._budget.model_copy(
            update={
                "budget": list(self._budget_rows),
                "suggestions": _suggestions_for_budget(self._context_cap, disk=self._disk_bound),
            }
        )
        self._budget, self._budget_rows, self._context_cap = None, [], None
        self._disk_bound = False
        return [ErrorEvent(error)]

    def _attach_plan(self, line: str) -> None:
        try:
            plan = json.loads(line.split(":", 1)[1])
        except ValueError:
            return
        context = plan.get("maximum_context_tokens") if isinstance(plan, dict) else None
        if isinstance(context, int) and context > 0 and self._context_cap is None:
            self._context_cap = context

    def _start_budget(self, line: str, code: str, message: str, cap: int | None) -> list[Event]:
        if self._budget is not None:
            update: dict[str, object] = {"raw": [*self._budget.raw, line]}
            if cap is not None:
                update.update(code=code, message=message)
                self._context_cap = cap
            self._budget = self._budget.model_copy(update=update)
            return []
        self._budget = EngineError(kind="budget_refusal", code=code, message=message, raw=[line])
        self._context_cap = cap
        return []

    def _line(self, line: str) -> list[Event]:
        lowered = line.lower()
        # A streamed store's size refused, before the weights load or by the plan made
        # with them: a setting, not the budget, so it is told apart from the memory
        # refusals below.
        if "runtime bootstrap failed" in lowered and (setting := _store_setting_error(line)):
            self._setting_refused = True
            if self._budget is not None:
                return [*self._end_budget(), ErrorEvent(setting)]
            return [ErrorEvent(setting)]
        # Memory budget refusal (RuntimeResources.mm / Bootstrap.mm:286-299 via main.mm).
        exceeds = _MAX_CONTEXT_EXCEEDS.search(line)
        if exceeds:
            cap = int(exceeds.group(3))
            # A block-sparse model's context follows the SSD cache's room for its KV.
            disk = "the ssd cache's room" in lowered
            bound = (
                "the SSD cache's room for its KV allows; free disk space or let the SSD cache grow"
                if disk
                else "the memory ceiling allows"
                if "--max-memory allow" in lowered
                else "this Mac's memory allows"
            )
            self._disk_bound = disk
            return self._start_budget(
                line,
                "max_context_exceeds",
                f"--max-context {int(exceeds.group(1)):,} is more than the {cap:,} tokens {bound}",
                cap,
            )
        if "memory budget validation failed" in lowered or (
            "runtime bootstrap failed" in lowered and "resource_assembly" in lowered
        ):
            return self._start_budget(
                line,
                "kv_pool_does_not_fit",
                "The model does not fit in memory with these settings",
                None,
            )
        # Ready · <model> · context 256K[ · language only] · http://… (server.py:1831); a
        # context that is no whole K, past 256K with YaRN too, reads "context 1,000,000".
        if line.startswith("Ready · "):
            parts = line.split(" · ")
            match = _CONTEXT.search(line)
            return [
                PhaseEvent("warming"),
                ReadyEvent(
                    model=parts[1].strip() if len(parts) > 1 else None,
                    context_tokens=parse_context(match.group(1)) if match else None,
                    language_only="language only" in lowered,
                ),
            ]
        if line.startswith("Loading · "):  # server.py:1762
            return [PhaseEvent("loading")]
        if line.startswith("Chat template · "):  # server.py:1773
            mode = template_mode(line)
            return [TemplateEvent(mode)] if mode else []
        # The weights are in; startup warmup follows, silent until "Ready" (a large
        # MoE model at a long context can take a minute or more). A start whose
        # streamed stores change after the first plan loads them again ("Weights
        # loaded again in"), still within warming.
        if _WEIGHTS_LOADED.search(line):  # RuntimeResources.mm:856
            return [PhaseEvent("warming")]
        # NativeRuntime.cpp:133 (idle release) and :373 (POST /v1/memory/release).
        if line.startswith(("Weights released after", "Weights released on request")):
            return [WeightsEvent("released")]
        restored = _WEIGHTS_RESTORED.search(line)
        if restored:  # NativeRuntime.cpp:143
            return [WeightsEvent("restored", float(restored.group(1)))]
        taken = _TAKEN_BACK.search(line)
        if taken:  # RuntimeResources.mm:1179
            return [TakenBackEvent(*(int(g) for g in taken.groups()))]
        # Why the SSD cache, or only its persistence, is off this start
        # (RuntimeResources.mm "SSD cache off: <reason>; …", "Persistent cache off: …").
        if line.startswith("SSD cache off: "):
            return [NoticeEvent("disk_cache_off", line)]
        if line.startswith("Persistent cache off: "):
            return [NoticeEvent("persistent_cache_off", line)]
        # RuntimeResources.mm:1167: the cache starts cold (the reason text varies).
        if line.startswith("Persistent cache emptied: "):
            return [NoticeEvent("persistent_cache_emptied", line)]
        # RuntimeResources.mm reportIgnoredSetting and the spill's ignored options.
        if _IGNORED_SETTING.match(line):
            return [NoticeEvent("setting_ignored", line)]
        # RuntimeResources.mm:1045-1049: a --max-context past the native window.
        if line.startswith("YaRN is on: "):
            return [NoticeEvent("yarn_on", line)]
        if line.startswith("Could not reach the Hub"):  # install/upstream.py:490/499/527
            return [NoticeEvent("hub_unreachable", line)]
        if line.startswith("Warning: keeping the installed"):  # upstream.py _keeping_installation
            return [NoticeEvent("new_commit_not_installed", line.removeprefix("Warning: "))]
        if line.startswith("Engine failed · "):  # backend.py
            return [TransportEvent("failed", line.removeprefix("Engine failed · "))]
        if line.startswith("Engine stopped · "):
            return [TransportEvent("stopped", line.removeprefix("Engine stopped · "))]
        if line.startswith("Engine restarted"):
            return [TransportEvent("restarted", line)]
        if (
            line.startswith(("Installing ", "Fetching ", "Selected ", "Updating ", "Reinstalling "))
            or " moved from " in line
        ):
            return [PhaseEvent("installing")]
        # Per-request log lines ("Error · code · POST /v1/…") are not startup errors.
        if line.startswith("Error · ") and " · " in line.removeprefix("Error · "):
            return []
        if lowered.startswith(("error:", "error · ")):
            error = classify_error(line)
            if error is None:
                error = EngineError(
                    kind="other",
                    code="startup_error",
                    message=re.sub(r"^(error:\s*|error · )", "", line, flags=re.IGNORECASE),
                    raw=[line],
                    suggestions=[EngineSuggestion(action="open_logs", label="Open logs")],
                )
            return [ErrorEvent(error)]
        error = classify_error(line) if "error" in lowered else None
        return [ErrorEvent(error)] if error is not None else []

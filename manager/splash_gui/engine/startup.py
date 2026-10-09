"""Engine output lines → structured events.

Every pattern below is a line Splash 1.2.0 prints; the source is noted beside
each. Matching is lenient (case-insensitive substrings where possible), the
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
_DISK_SUGGESTION = re.compile(r"may not hold a (\d+)-token request")

Phase = Literal["installing", "loading", "warming"]
TemplateMode = Literal["native", "patched", "unsupported"]
NoticeKind = Literal[
    "disk_tier_suggestion",
    "hub_unreachable",
    "new_commit_not_installed",
    "weights_restored",
    "schema_unrecognized",
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


def _suggestions_for_budget(context_cap: int | None) -> list[EngineSuggestion]:
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


def classify_error(text: str) -> EngineError | None:
    """An error line from the launcher, installer or server as an EngineError."""
    lowered = text.lower()
    message = re.sub(r"^(error:\s*|error · )", "", text, flags=re.IGNORECASE).strip()
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
        or "requires an mlx affine 4-bit" in lowered
        or explain_refusal(text)[1] is not None
        or "stores tensors splash cannot load" in lowered
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
        if "choose another variant" in lowered:
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

    def feed(self, raw_line: str) -> list[Event]:
        line = strip_stamp(raw_line)
        if not line.strip():
            return self._end_budget()
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
                "suggestions": _suggestions_for_budget(self._context_cap),
            }
        )
        self._budget, self._budget_rows, self._context_cap = None, [], None
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
        # Memory budget refusal (RuntimeResources.mm / Bootstrap.mm via main.mm).
        exceeds = _MAX_CONTEXT_EXCEEDS.search(line)
        if exceeds:
            cap = int(exceeds.group(3))
            return self._start_budget(
                line,
                "max_context_exceeds",
                f"--max-context {int(exceeds.group(1)):,} is more than the {cap:,} tokens "
                "this Mac's memory allows",
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
        # Ready · <model> · context 256K[ · language only] · http://… (server.py:2023)
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
        if line.startswith("Loading · "):  # server.py:1962
            return [PhaseEvent("loading")]
        if line.startswith("Chat template · "):  # server.py:1968
            mode = template_mode(line)
            return [TemplateEvent(mode)] if mode else []
        if _WEIGHTS_LOADED.search(line):  # RuntimeResources.mm:385
            return [PhaseEvent("loading")]
        if line.startswith("Weights released after"):  # NativeRuntime.cpp:133
            return [WeightsEvent("released")]
        restored = _WEIGHTS_RESTORED.search(line)
        if restored:  # NativeRuntime.cpp:143
            return [WeightsEvent("restored", float(restored.group(1)))]
        taken = _TAKEN_BACK.search(line)
        if taken:  # RuntimeResources.mm:564
            return [TakenBackEvent(*(int(g) for g in taken.groups()))]
        if _DISK_SUGGESTION.search(line):  # Bootstrap.mm:288
            return [NoticeEvent("disk_tier_suggestion", line)]
        if line.startswith("Could not reach the Hub"):  # upstream.py:148/330
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

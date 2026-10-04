"""Per-API-shape defaults/profile injection (SPEC §7.5) and usage capture (§7.6).

The injection table, field by field (a field is injected only when the request
leaves it out or sets it to null; it never overrides an explicit value):

| Overlay field          | Chat                  | Completions | Responses          | Messages |
|------------------------|-----------------------|-------------|--------------------|----------|
| temperature/top_p/top_k| ✓                     | ✓           | ✓                  | ✓        |
| min_p, penalties       | ✓                     | ✓           | ✓                  | ✗        |
| seed/priority/timeout  | ✓                     | ✓           | ✓                  | ✓        |
| stop                   | ✓                     | ✓           | ✓                  | ✗        |
| max_tokens             | max_completion_tokens | max_tokens  | max_output_tokens  | ✗        |
| reasoning_effort       | reasoning_effort      | ✗           | reasoning.effort   | ✗        |
| thinking               | ✗                     | ✗           | ✗                  | thinking |
| chat_template_kwargs   | ✓                     | ✗           | ✗                  | ✗        |
| ignore_eos             | ✓                     | ✓           | ✗                  | ✗        |

`stop` and `ignore_eos` are skipped for a request with tools or a structured
format, which Splash would refuse with them.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal

Shape = Literal["chat", "completions", "responses", "messages", "count_tokens", "other"]

SHAPES: dict[str, Shape] = {
    "/v1/chat/completions": "chat",
    "/v1/completions": "completions",
    "/v1/responses": "responses",
    "/v1/messages": "messages",
    "/v1/messages/count_tokens": "count_tokens",
}

_COMMON = ("temperature", "top_p", "top_k", "seed", "priority", "timeout")
_PENALTIES = ("min_p", "presence_penalty", "frequency_penalty", "repetition_penalty")
DIRECT_FIELDS: dict[Shape, tuple[str, ...]] = {
    "chat": (
        *_COMMON,
        *_PENALTIES,
        "stop",
        "chat_template_kwargs",
        "ignore_eos",
        "reasoning_effort",
    ),
    "completions": (*_COMMON, *_PENALTIES, "stop", "ignore_eos"),
    "responses": (*_COMMON, *_PENALTIES, "stop"),
    "messages": (*_COMMON, "thinking"),
}
MAX_TOKENS_FIELD: dict[Shape, str] = {
    "chat": "max_completion_tokens",
    "completions": "max_tokens",
    "responses": "max_output_tokens",
}


def _missing(body: dict[str, Any], name: str) -> bool:
    return body.get(name) is None


# Splash refuses these next to tools or structured output ("stop cannot be combined
# with tools or structured output", "ignore_eos cannot be …"; server/frontend.py
# around the `constrained` check), so a default never adds them to such a request.
UNCONSTRAINED_ONLY = ("stop", "ignore_eos")


def constrained(shape: Shape, body: dict[str, Any]) -> bool:
    """Whether the request generates under a grammar: tools or a structured format."""
    if body.get("tools"):
        return True
    if shape == "responses":
        text = body.get("text")
        fmt = text.get("format") if isinstance(text, dict) else None
    else:
        fmt = body.get("response_format")
    if isinstance(fmt, dict):
        return fmt.get("type") not in (None, "text")
    return False


def inject(
    shape: Shape, body: dict[str, Any], overlay: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fill the fields the request omits from `overlay`. Returns (body, injected)."""
    if shape not in DIRECT_FIELDS or not overlay:
        return body, {}
    out = dict(body)
    injected: dict[str, Any] = {}
    grammar = constrained(shape, body)
    for name in DIRECT_FIELDS[shape]:
        if grammar and name in UNCONSTRAINED_ONLY:
            continue
        if name in overlay and overlay[name] is not None and _missing(out, name):
            out[name] = overlay[name]
            injected[name] = overlay[name]
    max_tokens = overlay.get("max_tokens")
    target = MAX_TOKENS_FIELD.get(shape)
    if max_tokens is not None and target is not None:
        # Chat accepts either spelling; an explicit one of them wins.
        names = (target, "max_tokens") if shape == "chat" else (target,)
        if all(_missing(out, n) for n in names):
            out[target] = max_tokens
            injected[target] = max_tokens
    effort = overlay.get("reasoning_effort")
    if shape == "responses" and effort is not None:
        reasoning = out.get("reasoning")
        if reasoning is None:
            out["reasoning"] = {"effort": effort}
            injected["reasoning.effort"] = effort
        elif isinstance(reasoning, dict) and reasoning.get("effort") is None:
            out["reasoning"] = {**reasoning, "effort": effort}
            injected["reasoning.effort"] = effort
    return out, injected


# Usage capture ------------------------------------------------------------------


@dataclass
class UsageCapture:
    """Reads usage, timings and errors from a response as it streams past."""

    shape: Shape
    endpoint: str
    started: float = field(default_factory=time.monotonic)
    stream: bool = False
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    completion_tokens: int | None = None
    prompt_ms: float | None = None
    predicted_ms: float | None = None
    ttft_ms: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    finish_reason: str | None = None
    _buffer: bytearray = field(default_factory=bytearray)
    _body: bytearray = field(default_factory=bytearray)
    _event: str | None = None
    MAX_BODY = 8 * 1024 * 1024

    def feed(self, chunk: bytes) -> None:
        if not self.stream:
            if len(self._body) < self.MAX_BODY:
                self._body.extend(chunk[: self.MAX_BODY - len(self._body)])
            return
        self._buffer.extend(chunk)
        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                break
            line = bytes(self._buffer[:index]).rstrip(b"\r")
            del self._buffer[: index + 1]
            self._line(line.decode("utf-8", "replace"))

    def finish(self) -> None:
        if self.stream:
            if self._buffer:
                self._line(bytes(self._buffer).decode("utf-8", "replace"))
                self._buffer.clear()
            return
        try:
            data = json.loads(bytes(self._body))
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(data, dict):
            self._object(data, final=True)

    def _line(self, line: str) -> None:
        if not line:
            self._event = None
            return
        if line.startswith("event:"):
            self._event = line[6:].strip()
            return
        if not line.startswith("data:"):
            return
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            data = json.loads(payload)
        except ValueError:
            return
        if isinstance(data, dict):
            self._object(data, final=False)

    def _first_token(self) -> None:
        if self.ttft_ms is None:
            self.ttft_ms = (time.monotonic() - self.started) * 1000

    def _object(self, data: dict[str, Any], *, final: bool) -> None:
        error = data.get("error")
        if isinstance(error, dict) and data.get("type") != "response.failed":
            self.error_code = str(error.get("code") or error.get("type") or "error")
            if isinstance(error.get("message"), str):
                self.error_message = error["message"][:500]
        kind = data.get("type")
        if self.shape == "count_tokens" and isinstance(data.get("input_tokens"), int):
            self.prompt_tokens = int(data["input_tokens"])
        if self.shape in ("chat", "completions", "other", "count_tokens"):
            self._openai(data, final)
        if self.shape == "responses" or (isinstance(kind, str) and kind.startswith("response.")):
            self._responses(data, final)
        if self.shape in ("messages", "count_tokens") or kind in (
            "message_start",
            "message_delta",
            "content_block_delta",
            "message",
        ):
            self._messages(data, final)

    def _openai(self, data: dict[str, Any], final: bool) -> None:
        choices = data.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta") if self.stream else choice.get("message")
                text = choice.get("text")
                if (
                    isinstance(delta, dict)
                    and any(delta.get(k) for k in ("content", "reasoning_content", "tool_calls"))
                ) or (isinstance(text, str) and text):
                    self._first_token()
                if choice.get("finish_reason"):
                    self.finish_reason = str(choice["finish_reason"])
        timings = data.get("timings")
        if isinstance(timings, dict):
            self.prompt_ms = _f(timings.get("prompt_ms"), self.prompt_ms)
            self.predicted_ms = _f(timings.get("predicted_ms"), self.predicted_ms)
            # llama-server-style timings (server/metrics.py) carry the token counts
            # even when the client did not ask for `stream_options.include_usage`.
            cache_n = timings.get("cache_n")
            if isinstance(cache_n, int) and self.cached_tokens is None:
                self.cached_tokens = cache_n
            prompt_n = timings.get("prompt_n")
            if isinstance(prompt_n, int) and self.prompt_tokens is None:
                self.prompt_tokens = prompt_n
            predicted_n = timings.get("predicted_n")
            if isinstance(predicted_n, int) and self.completion_tokens is None:
                self.completion_tokens = predicted_n
        usage = data.get("usage")
        if isinstance(usage, dict):
            self.prompt_tokens = _i(usage.get("prompt_tokens"), self.prompt_tokens)
            self.completion_tokens = _i(usage.get("completion_tokens"), self.completion_tokens)
            details = usage.get("prompt_tokens_details")
            if isinstance(details, dict):
                self.cached_tokens = _i(details.get("cached_tokens"), self.cached_tokens)
            # /v1/judgments, /v1/systemone and count_tokens report other names.
            self.prompt_tokens = _i(usage.get("input_tokens"), self.prompt_tokens)

    def _responses(self, data: dict[str, Any], final: bool) -> None:
        kind = data.get("type")
        if isinstance(kind, str) and kind.endswith(".delta"):
            self._first_token()
        response: Any = data.get("response") if isinstance(data.get("response"), dict) else None
        if response is None and data.get("object") == "response":
            response = data
        if kind == "response.failed" and isinstance(response, dict):
            error = response.get("error")
            if isinstance(error, dict):
                self.error_code = str(error.get("code") or "response_failed")
        if final and isinstance(response, dict) and response.get("output"):
            # A non-streamed response: its first token arrived with the whole body,
            # as for Chat and Messages.
            self._first_token()
        if isinstance(response, dict):
            usage = response.get("usage")
            if isinstance(usage, dict):
                self.prompt_tokens = _i(usage.get("input_tokens"), self.prompt_tokens)
                self.completion_tokens = _i(usage.get("output_tokens"), self.completion_tokens)
                details = usage.get("input_tokens_details")
                if isinstance(details, dict):
                    self.cached_tokens = _i(details.get("cached_tokens"), self.cached_tokens)
            status = response.get("status")
            if isinstance(status, str) and status not in ("in_progress", "queued"):
                self.finish_reason = status

    def _messages(self, data: dict[str, Any], final: bool) -> None:
        kind = data.get("type")
        if kind == "error" and isinstance(data.get("error"), dict):
            self.error_code = str(data["error"].get("type") or "error")
        if kind == "content_block_delta":
            self._first_token()
        message = data.get("message") if kind == "message_start" else None
        if message is None and kind == "message":
            message = data
            if data.get("content"):
                self._first_token()
        usage = data.get("usage")
        if isinstance(message, dict):
            usage = message.get("usage")
            if message.get("stop_reason"):
                self.finish_reason = str(message["stop_reason"])
        if kind == "message_delta" and isinstance(data.get("delta"), dict):
            reason = data["delta"].get("stop_reason")
            if reason:
                self.finish_reason = str(reason)
        if isinstance(usage, dict):
            self.prompt_tokens = _i(usage.get("input_tokens"), self.prompt_tokens)
            self.cached_tokens = _i(usage.get("cache_read_input_tokens"), self.cached_tokens)
            output = usage.get("output_tokens")
            if isinstance(output, int) and (kind != "message_start" or output):
                self.completion_tokens = output


def _i(value: Any, current: int | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else current


def _f(value: Any, current: float | None) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return current


def guess_client(user_agent: str | None, same_origin: bool = False) -> str | None:
    """A short client name from the User-Agent (SPEC §7.6)."""
    ua = (user_agent or "").lower()
    if not ua:
        return None
    table = (
        ("claude-cli", "claude-code"),
        ("claude-code", "claude-code"),
        ("claude/", "claude-desktop"),
        ("codex", "codex"),
        ("opencode", "opencode"),
        ("hermes", "hermes"),
        ("pi-coding-agent", "pi"),
        ("typesafe", "typesafe-sdk"),
        ("splash-gui-cli", "splash-cli"),
        ("splash-gui", "splash-gui"),
        ("anthropic/python", "anthropic-python"),
        ("anthropic/js", "anthropic-js"),
        ("openai/python", "openai-python"),
        ("openai/js", "openai-js"),
        ("curl/", "curl"),
        ("python-httpx", "httpx"),
        ("python-requests", "requests"),
        ("python-urllib", "python"),
        ("lm studio", "lm-studio"),
        ("jan/", "jan"),
    )
    for needle, name in table:
        if needle in ua:
            return name
    if "mozilla" in ua:
        return "splash-gui" if same_origin else "browser"
    return ua.split("/", 1)[0][:40] or None

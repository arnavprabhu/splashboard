"""Response and stream-chunk shapes, copied from splash/server/api_shapes.py
(1.2.0, lines 982-1255) with the tool-policy and thinking-codec objects
replaced by plain fields of FakeJob. Keep these in step with the engine."""

from __future__ import annotations

import functools
import json
import time
from dataclasses import dataclass, field

from .metrics import metrics_dict, timings_dict, usage_dict


@dataclass(frozen=True)
class CacheInfo:
    status: str = "unknown"
    matched_tokens: int = 0
    lane: int = -1


@dataclass
class NativeResult:
    """Attribute-compatible with server/backend.py NativeResult, which the
    copied metrics.py reads."""

    reason: str
    prompt_tokens: int
    completion_tokens: int
    start_to_first_token_ms: float
    first_token_to_done_ms: float
    request_wall_ms: float
    prefill_tokens: int = 0
    cache: CacheInfo = field(default_factory=CacheInfo)
    stop_sequence: str | None = None
    first_token_batch_tokens: int = 0
    option_logits: tuple = ()

    @functools.cached_property
    def metrics(self) -> dict:
        return metrics_dict(self)


@dataclass
class FakeJob:
    public_id: str
    prompt_tokens: list[int]
    prompt_text: str
    max_new_tokens: int
    created_at: int = field(default_factory=lambda: int(time.time()))
    thinking: bool = False
    thinking_display: str = "summarized"
    reasoning_tokens: int = 0
    stop_sequences: tuple[str, ...] = ()
    ignore_eos: bool = False
    tools: list | None = None
    tool_choice: object = None
    parallel_tool_calls: bool = True
    # api_shapes.py normalize_responses_tools (1.3.0): Splash's alias of each
    # namespaced tool -> (namespace, name), read back into function_call items.
    tool_namespaces: dict = field(default_factory=dict)
    response_format: dict | None = None
    response_store: bool = False
    response_previous_id: str | None = None
    response_history_items: list | None = None
    output_clamped_to_context: bool = False
    priority: str = "normal"
    deadline: float = float("inf")
    return_progress: bool = False
    last_user: str = ""
    score_options: int = 0


@dataclass
class Block:
    kind: str  # reasoning | text | tool
    text: str = ""
    status: str = "in_progress"
    call_id: str | None = None
    name: str | None = None


class BlockSequencer:
    """Groups streamed output into blocks, opening one per change of kind,
    like server/output.py BlockSequencer."""

    def __init__(self, on_open=None, on_delta=None, on_close=None):
        self.blocks: list[Block] = []
        self.on_open, self.on_delta, self.on_close = on_open, on_delta, on_close

    @property
    def current(self) -> Block | None:
        if self.blocks and self.blocks[-1].status == "in_progress":
            return self.blocks[-1]
        return None

    def _close(self, status: str = "completed") -> None:
        block = self.current
        if block is None:
            return
        block.status = status
        if self.on_close:
            self.on_close(len(self.blocks) - 1, block)

    def _open(self, block: Block) -> None:
        self._close()
        self.blocks.append(block)
        if self.on_open:
            self.on_open(len(self.blocks) - 1, block)

    def text(self, field_name: str, text: str) -> None:
        kind = "reasoning" if field_name == "reasoning_content" else "text"
        if self.current is None or self.current.kind != kind:
            self._open(Block(kind))
        block = self.current
        assert block is not None
        block.text += text
        if self.on_delta:
            self.on_delta(len(self.blocks) - 1, block, text)

    def tool(self, call_id: str, name: str, arguments_parts: list[str]) -> None:
        self._open(Block("tool", call_id=call_id, name=name))
        block = self.current
        assert block is not None
        for part in arguments_parts:
            block.text += part
            if self.on_delta:
                self.on_delta(len(self.blocks) - 1, block, part)

    def finish(self, incomplete: bool) -> list[Block]:
        self._close("incomplete" if incomplete else "completed")
        return self.blocks


def responses_response(model, job, status, output, result=None, error=None):
    usage = None
    if result is not None:
        cache = result.cache
        usage = {
            "input_tokens": result.prompt_tokens,
            "input_tokens_details": {
                "cached_tokens": cache.matched_tokens,
                "cache_write_tokens": max(0, result.prompt_tokens - cache.matched_tokens),
            },
            "output_tokens": result.completion_tokens,
            "output_tokens_details": {"reasoning_tokens": job.reasoning_tokens},
            "total_tokens": result.prompt_tokens + result.completion_tokens,
        }
    text_format = job.response_format or {"type": "text"}
    if text_format.get("type") == "json_schema":
        text_format = {"type": "json_schema", **text_format.get("json_schema", {})}
    return {
        "id": f"resp_{job.public_id}",
        "object": "response",
        "created_at": job.created_at,
        "status": status,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "error": error,
        "model": model,
        "output": output,
        "parallel_tool_calls": job.parallel_tool_calls,
        "store": job.response_store,
        "previous_response_id": job.response_previous_id,
        "text": {"format": text_format},
        "usage": usage,
        "end_turn": result is not None
        and result.reason == "stop"
        and not any(item["type"] == "function_call" for item in output),
    }


def finish_reason(result, tool_calls):
    if tool_calls and result.reason != "length":
        return "tool_calls"
    return result.reason


def completion_response(model, job, result, message, tool_calls):
    return {
        "id": f"chatcmpl-{job.public_id}",
        "object": "chat.completion",
        "created": job.created_at,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason(result, tool_calls),
            }
        ],
        "usage": usage_dict(result, job),
        "metrics": result.metrics,
        "timings": timings_dict(result),
    }


def text_completion_response(model, job, result, text):
    return {
        "id": f"cmpl-{job.public_id}",
        "object": "text_completion",
        "created": job.created_at,
        "model": model,
        "choices": [{"index": 0, "text": text, "logprobs": None, "finish_reason": result.reason}],
        "usage": usage_dict(result, job),
        "metrics": result.metrics,
        "timings": timings_dict(result),
    }


def _chunk(object_type, chunk_id, created, model, choice, usage, metrics, timings):
    chunk = {
        "id": chunk_id,
        "object": object_type,
        "created": created,
        "model": model,
        "choices": [choice],
    }
    if usage is not None:
        chunk["choices"] = []
        chunk["usage"] = usage
    if metrics is not None:
        chunk["metrics"] = metrics
    if timings is not None:
        chunk["timings"] = timings
    return chunk


def stream_chunk(model, request_id, created, delta, finish_reason=None, usage=None, metrics=None, timings=None):
    return _chunk(
        "chat.completion.chunk",
        f"chatcmpl-{request_id}",
        created,
        model,
        {"index": 0, "delta": delta, "finish_reason": finish_reason},
        usage,
        metrics,
        timings,
    )


def text_completion_chunk(model, request_id, created, text, finish_reason=None, usage=None, metrics=None, timings=None):
    return _chunk(
        "text_completion",
        f"cmpl-{request_id}",
        created,
        model,
        {"index": 0, "text": text, "logprobs": None, "finish_reason": finish_reason},
        usage,
        metrics,
        timings,
    )


_RESPONSES_ITEM_PREFIXES = {"reasoning": "rs", "text": "msg", "tool": "fc"}


def responses_item_id(job, kind, index):
    return f"{_RESPONSES_ITEM_PREFIXES[kind]}_{job.public_id}_{index}"


def responses_item(job, block, index):
    item_id = responses_item_id(job, block.kind, index)
    status = block.status
    text = "" if status == "in_progress" else block.text
    if block.kind == "reasoning":
        return {
            "id": item_id,
            "type": "reasoning",
            "status": status,
            "summary": [{"type": "summary_text", "text": text}] if text else [],
            "content": [{"type": "reasoning_text", "text": text}] if text else [],
            "encrypted_content": None,
        }
    if block.kind == "text":
        return {
            "id": item_id,
            "type": "message",
            "status": status,
            "role": "assistant",
            "content": [] if status == "in_progress" else [{"type": "output_text", "text": text, "annotations": []}],
        }
    namespaced = job.tool_namespaces.get(block.name)
    item = {
        "id": item_id,
        "type": "function_call",
        "status": status,
        "call_id": block.call_id,
        "name": namespaced[1] if namespaced else block.name,
        "arguments": text,
    }
    if namespaced:
        item["namespace"] = namespaced[0]
    return item


def responses_output(job, blocks):
    return [responses_item(job, block, index) for index, block in enumerate(blocks)]


def anthropic_stop(result, tool_calls, output_clamped_to_context):
    if tool_calls and result.reason != "length":
        return "tool_use"
    if result.reason == "length":
        if output_clamped_to_context:
            return "model_context_window_exceeded"
        return "max_tokens"
    if result.stop_sequence is not None:
        return "stop_sequence"
    return "end_turn"


def anthropic_usage(prompt_tokens, output_tokens, cache):
    return {
        "input_tokens": prompt_tokens - cache.matched_tokens,
        "cache_read_input_tokens": cache.matched_tokens,
        "output_tokens": output_tokens,
    }


def anthropic_response(model, job, blocks, result, tool_calls, thinking_signature):
    content = []
    for block in blocks:
        if block.kind == "reasoning":
            content.append(
                {
                    "type": "thinking",
                    "thinking": "" if job.thinking_display == "omitted" else block.text,
                    "signature": thinking_signature,
                }
            )
        elif block.kind == "text":
            content.append({"type": "text", "text": block.text})
        else:
            try:
                arguments = json.loads(block.text)
            except ValueError:
                if result.reason != "length":
                    raise
                continue
            content.append({"type": "tool_use", "id": block.call_id, "name": block.name, "input": arguments})
    if all(item["type"] == "thinking" for item in content):
        content.append({"type": "text", "text": ""})
    return {
        "id": f"msg_{job.public_id}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": anthropic_stop(result, tool_calls, job.output_clamped_to_context),
        "stop_sequence": result.stop_sequence,
        "usage": anthropic_usage(result.prompt_tokens, result.completion_tokens, result.cache),
    }

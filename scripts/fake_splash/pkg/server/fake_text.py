"""Deterministic fake tokenization, chat templating and reply text.

Nothing here models Qwen's real tokenizer: a "token" is one whitespace-led
word piece, and ids are a stable hash of it. That is enough for the GUI to see
plausible, repeatable counts, prefix-cache hits and streamed deltas.
"""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass

VOCAB_SIZE = 248320
IM_START, IM_END = "<|im_start|>", "<|im_end|>"
_PIECE = re.compile(r"\s*\S+|\s+")
_FILLER = [
    "Splash",
    "serves",
    "one",
    "model",
    "per",
    "process",
    "and",
    "keeps",
    "a",
    "prefix",
    "cache",
    "so",
    "repeated",
    "conversation",
    "turns",
    "reuse",
    "their",
    "earlier",
    "work",
    "instead",
    "of",
    "computing",
    "it",
    "again.",
]


def pieces(text: str) -> list[str]:
    return _PIECE.findall(text)


def token_id(piece: str) -> int:
    return zlib.crc32(piece.encode("utf-8")) % VOCAB_SIZE


def tokenize(text: str) -> list[int]:
    return [token_id(piece) for piece in pieces(text)]


def content_text(content: object) -> str:
    """Plain text of a Chat, Responses or Messages content value."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                kind = part.get("type")
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif kind in ("image_url", "input_image", "image"):
                    parts.append("<|vision_start|><|image_pad|><|vision_end|>")
                elif kind in ("file", "input_file", "document"):
                    parts.append("[document]")
                elif kind == "tool_result":
                    parts.append(content_text(part.get("content")))
        return "".join(parts)
    if isinstance(content, dict):
        return content_text([content])
    return json.dumps(content)


@dataclass
class Rendered:
    text: str
    tokens: list[int]
    thinking: bool
    last_user: str


def render_chat(
    messages: list[dict],
    *,
    tools: list | None = None,
    thinking: bool = True,
    add_generation_prompt: bool = True,
) -> Rendered:
    """A Qwen-style ChatML rendering of normalized role/content messages."""
    out: list[str] = []
    system = [content_text(m.get("content")) for m in messages if m.get("role") in ("system", "developer")]
    if tools:
        tool_text = "\n".join(json.dumps(tool, sort_keys=True) for tool in tools)
        system.append("# Tools\n<tools>\n" + tool_text + "\n</tools>")
    if system:
        out.append(f"{IM_START}system\n" + "\n\n".join(system) + f"{IM_END}\n")
    last_user = ""
    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            continue
        text = content_text(message.get("content"))
        if role == "user":
            last_user = text
        if role == "assistant" and message.get("tool_calls"):
            calls = "".join(
                "<tool_call>\n" + json.dumps(call.get("function", call)) + "\n</tool_call>"
                for call in message["tool_calls"]
            )
            text += calls
        out.append(f"{IM_START}{'user' if role == 'tool' else role}\n{text}{IM_END}\n")
    if add_generation_prompt:
        out.append(f"{IM_START}assistant\n" + ("<think>\n" if thinking else "<think>\n\n</think>\n\n"))
    text = "".join(out)
    return Rendered(text, tokenize(text), thinking, last_user)


def snippet(text: str, words: int = 12) -> str:
    found = text.split()
    short = " ".join(found[:words])
    return short + ("…" if len(found) > words else "")


def reply_pieces(prompt: str, override: str | None, reply_tokens: int | None) -> list[str]:
    """The fake answer's pieces: an override text, an exact token count of
    filler, or a short echo of the prompt."""
    if override:
        return pieces(override)
    if reply_tokens is not None:
        return [(" " if i else "") + _FILLER[i % len(_FILLER)] for i in range(reply_tokens)]
    subject = snippet(prompt) or "your request"
    return pieces(f"This is a fake reply from Splash to: {subject}")


def reasoning_pieces(prompt: str) -> list[str]:
    return pieces(f"The user asked about {snippet(prompt, 6) or 'something'}. A short answer fits.")


def tool_arguments(tool: dict, prompt: str) -> str:
    """Arguments satisfying the tool's required parameters, compact JSON."""
    function = tool.get("function", tool)
    schema = function.get("parameters") or function.get("input_schema") or {}
    properties = schema.get("properties") or {}
    args: dict[str, object] = {}
    for name in schema.get("required") or []:
        kind = (properties.get(name) or {}).get("type")
        if kind == "string":
            args[name] = snippet(prompt, 6) or "fake"
        elif kind in ("number", "integer"):
            args[name] = 1
        elif kind == "boolean":
            args[name] = True
        elif kind == "array":
            args[name] = []
        else:
            args[name] = {}
    return json.dumps(args, separators=(",", ":"), ensure_ascii=False)


def tool_name(tool: dict) -> str:
    function = tool.get("function", tool)
    return str(function.get("name", "tool"))

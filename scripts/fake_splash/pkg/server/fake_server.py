"""Fake `splash serve` HTTP server: the routes, error shapes, streaming
formats, auth and Host checks of splash/server/server.py over the simulated
engine in fake_engine.py.

Control endpoints (no auth, not part of Splash):
  GET    /_fake/state           mode, config, counters, argv, selected env
  GET    /_fake/last_requests   recorded requests (?n=10), newest last
  DELETE /_fake/last_requests   clear the record
  POST   /_fake/mode            {"mode": "...", "config": {...}}
  POST   /_fake/crash           {"code": 1} or {"signal": "SIGKILL"}
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import select
import signal
import socket
import socketserver
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import fake_text, json_codec, serve_options
from .errors import APIError, ContextLengthError
from .fake_engine import (
    LATER_SYSTEM_UNSUPPORTED,
    FakeConfig,
    FakeEngine,
    ServeSettings,
    StartupFailed,
    print_status,
    write_stderr_line,
)
from .fake_shapes import (
    BlockSequencer,
    FakeJob,
    NativeResult,
    anthropic_response,
    anthropic_stop,
    anthropic_usage,
    completion_response,
    finish_reason,
    responses_item,
    responses_item_id,
    responses_output,
    responses_response,
    stream_chunk,
    text_completion_chunk,
    text_completion_response,
)
from .http_security import authenticate, validate_headers
from .metrics import prometheus_metrics, timings_dict, usage_dict
from .origins import ANY_ORIGIN

POST_ROUTES = (
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/responses",
    "/v1/messages",
    "/v1/messages/count_tokens",
    "/tokenize",
    "/apply-template",
    "/v1/judgments",
    "/v1/systemone",
)
PUBLIC_GET = ("/", "/index.html", "/favicon.ico", "/health", "/ready")
REASONING_EFFORTS = serve_options.REASONING_EFFORTS
PRIORITIES = ("foreground", "normal", "background")
MIN_FLOAT32_SUBNORMAL = float.fromhex("0x1p-149")
FLOAT32_MAX = float.fromhex("0x1.fffffep127")
# server/frontend.py SAMPLING_NUMBERS
SAMPLING_NUMBERS = {
    "temperature": (1.0, lambda v: 0 <= v <= 2, "a number in [0, 2]"),
    "top_p": (0.95, lambda v: MIN_FLOAT32_SUBNORMAL <= v <= 1, "a number in (0, 1]"),
    "presence_penalty": (0.0, lambda v: -2 <= v <= 2, "a number in [-2, 2]"),
    "frequency_penalty": (0.0, lambda v: -2 <= v <= 2, "a number in [-2, 2]"),
    "repetition_penalty": (1.0, lambda v: MIN_FLOAT32_SUBNORMAL <= v <= FLOAT32_MAX, "a positive number"),
    "min_p": (0.0, lambda v: 0 <= v <= 1, "a number in [0, 1]"),
}
SAMPLING_FIELDS = (*SAMPLING_NUMBERS, "top_k")
RESERVED_TEMPLATE_KWARGS = frozenset(
    {
        "add_generation_prompt",
        "chat_template",
        "continue_final_message",
        "conversation",
        "documents",
        "messages",
        "return_dict",
        "tokenize",
        "tools",
    }
)
COMPLETION_DEFAULT_MAX_TOKENS = 16
# server/api_shapes.py VISION_UNAVAILABLE
VISION_UNAVAILABLE = "this model is serving without vision (started with --language-only)"
# server/judgments.py constants
LETTERS = "ABCDEFGHIJKLMNOP"
DIRECT_SYSTEM = (
    "Apply the supplied criterion to the supplied evidence. Choose exactly one "
    "listed option. Respond with only its uppercase letter, with no explanation "
    "or reasoning."
)
PROMPT_VERSION = "direct-options-v1"
READOUT = "native full-vocabulary last-position logits restricted to declared answer slots"
PROBABILITY_STATUS = "conditional option score; uncalibrated as decision confidence"
CLIENT_DISCONNECT_POLL = 0.1


def is_finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def normalize_path(raw_path: str) -> str:
    """server/server.py _normalize_path."""
    decoded = unquote(raw_path.partition("?")[0].partition("#")[0])
    if not decoded.startswith("/"):
        return decoded
    trailing = decoded.endswith("/") and len(decoded) > 1
    segments: list[str] = []
    for segment in decoded.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    normalized = "/" + "/".join(segments)
    if trailing and normalized != "/":
        normalized += "/"
    return normalized


def softmax(values: list[float]) -> list[float]:
    maximum = max(values)
    weights = [math.exp(v - maximum) for v in values]
    total = sum(weights)
    return [w / total for w in weights]


def concentration(probabilities: list[float]) -> float:
    if len(probabilities) < 2:
        return 1.0
    entropy = -sum(p * math.log(p) for p in probabilities if p > 0.0)
    return max(0.0, 1.0 - entropy / math.log(len(probabilities)))


class SystemOneError(Exception):
    def __init__(self, details: list[dict]):
        self.details = details
        super().__init__(details[0]["msg"] if details else "invalid")


def detail(loc: list, msg: str, error_type: str = "value_error") -> dict:
    return {"loc": ["body", *loc], "msg": msg, "type": error_type}


class Recorder:
    def __init__(self, limit: int = 200):
        self.items: deque[dict] = deque(maxlen=limit)
        self.lock = threading.Lock()

    def add(self, item: dict) -> None:
        with self.lock:
            self.items.append(item)

    def list(self, n: int | None = None) -> list[dict]:
        with self.lock:
            items = list(self.items)
        return items[-n:] if n else items

    def clear(self) -> None:
        with self.lock:
            self.items.clear()


class FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    methods = "GET, HEAD, POST, DELETE, OPTIONS"
    server: FakeServer

    # --- plumbing ------------------------------------------------------------
    def setup(self) -> None:
        self._response_started = False
        self._allow_origin: str | None = None
        self._last_sse_write = time.monotonic()
        self._record: dict | None = None
        super().setup()

    def log_message(self, format: str, *args: object) -> None:
        pass

    def version_string(self) -> str:
        return "Splash"

    @property
    def engine(self) -> FakeEngine:
        return self.server.engine

    @property
    def route(self) -> str:
        return normalize_path(getattr(self, "path", ""))

    @property
    def anthropic(self) -> bool:
        return self.route.startswith("/v1/messages")

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        self.engine.connections.acquire()
        self._connection_counted = True
        if self.route.startswith("/_fake/"):
            return True
        self._record = {
            "time": time.time(),
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": None,
        }
        self.server.recorder.add(self._record)
        try:
            hosts = self.server.allowed_hosts | {self.connection.getsockname()[0].lower()}
            self._allow_origin = validate_headers(self.headers, hosts, self.server.allowed_origins)
            public = self.command == "OPTIONS" or (self.command in ("GET", "HEAD") and self.route in PUBLIC_GET)
            if not public:
                authenticate(self.headers, self.server.api_key)
        except APIError as error:
            self.close_connection = True
            self._safe_error(error, log=False)
            return False
        return True

    def finish(self) -> None:
        if getattr(self, "_connection_counted", False):
            self.engine.connections.release()
        with contextlib.suppress(OSError):
            super().finish()

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.request_version = self.protocol_version
        self._safe_error(APIError(code, message or self.responses[code][0]), log=False)

    def end_headers(self) -> None:
        if self._allow_origin is not None:
            self.send_header("Access-Control-Allow-Origin", self._allow_origin)
            if self._allow_origin != ANY_ORIGIN:
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Expose-Headers", "Retry-After, WWW-Authenticate")
        super().end_headers()

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        if status == 503:
            self.send_header("Retry-After", "1")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        route = self.route
        if route == "/v1/systemone" or route == "/v1/models" or route.startswith("/v1/models/"):
            self.send_header("x-typesafe-request-id", f"req_{secrets.token_hex(12)}")
        self._response_started = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        if self._record is not None:
            self._record["status"] = status

    def _json(self, status: int, payload: object) -> None:
        self._send(status, json_codec.encode(payload), "application/json")

    def _error(self, error: APIError) -> None:
        """server/server.py FrontendHandler._error."""
        anthropic = self.anthropic
        error_type = error.protocol_type(anthropic)
        message = error.message
        if anthropic and isinstance(error, ContextLengthError):
            message = (
                f"prompt is too long: {error.input_tokens} tokens > {error.maximum_input_tokens} maximum input tokens"
            )
        self._json(
            error.status,
            {"type": "error", "error": {"type": error_type, "message": message}}
            if anthropic
            else {"error": {"message": error.message, "type": error_type, "code": error.code}},
        )

    def _log_api_error(self, error: APIError) -> None:
        path = "".join(c if c.isprintable() else "?" for c in self.route)
        print_status(f"Error · {error.code} · {self.command} {path[:256]}", error=True)

    def _safe_error(self, error: APIError, *, log: bool = True) -> None:
        if self._response_started:
            return
        if log:
            self._log_api_error(error)
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, TimeoutError):
            self._error(error)

    def _read_json_body(self) -> object:
        """server/server.py _read_json_body, with the same refusals."""
        if self.headers.get_all("Transfer-Encoding"):
            raise APIError(400, "transfer encoding is not supported")
        encodings = self.headers.get_all("Content-Encoding", [])
        if len(encodings) > 1 or (encodings and encodings[0].strip().lower() != "identity"):
            raise APIError(415, "content encoding is not supported")
        content_types = self.headers.get_all("Content-Type", [])
        content_type = self.headers.get_content_type().lower()
        if len(content_types) != 1 or not (
            content_type == "application/json"
            or (content_type.startswith("application/") and content_type.endswith("+json"))
        ):
            raise APIError(415, "Content-Type must be application/json")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            raise APIError(400, "exactly one Content-Length header is required")
        if not lengths[0].isascii() or not lengths[0].isdigit():
            raise APIError(400, "invalid Content-Length header")
        length = int(lengths[0])
        if length <= 0:
            raise APIError(400, "request body must not be empty")
        limit = self.engine.settings.max_request_size
        if length > limit:
            raise APIError(
                413,
                f"request body is {length} bytes; limit is {limit} bytes (--max-request-size)",
                "request_too_large",
            )
        payload = self.rfile.read(length)
        if len(payload) < length:
            raise APIError(400, "request body ended before Content-Length")
        try:
            body = json_codec.loads(payload.decode(json.detect_encoding(payload), "surrogatepass"))
        except (ValueError, RecursionError) as error:
            raise APIError(400, "invalid JSON request body") from error
        if self._record is not None:
            self._record["body"] = body
        return body

    def _client_disconnected(self) -> bool:
        poller = select.poll()
        poller.register(self.connection, select.POLLIN)
        try:
            return bool(poller.poll(0)) and not self.connection.recv(65536, socket.MSG_DONTWAIT)
        except BlockingIOError:
            return False
        except ConnectionError:
            return True

    # --- SSE ---------------------------------------------------------------------
    def _start_event_stream(self) -> None:
        if self._response_started:
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self._response_started = True
        if self._record is not None:
            self._record["status"] = 200

    def _write_sse(self, frame: bytes) -> None:
        self.wfile.write(frame)
        self.wfile.flush()
        self._last_sse_write = time.monotonic()

    def _sse(self, payload: object) -> None:
        data = payload.encode() if isinstance(payload, str) else json_codec.encode(payload)
        self._write_sse(b"data: " + data + b"\n\n")

    def _event_sse(self, event: str, payload: dict) -> None:
        self._write_sse(f"event: {event}\ndata: ".encode() + json_codec.encode(payload) + b"\n\n")

    def _sse_keepalive(self) -> None:
        self._start_event_stream()
        self._write_sse(b": splash-keepalive\n\n")

    def _sse_error(self, error: APIError) -> None:
        self._sse({"error": {"message": error.message, "type": error.protocol_type(), "code": error.code}})
        self._sse("[DONE]")

    # --- GET / DELETE / OPTIONS ---------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", self.methods)
        if self._allow_origin is not None and "Access-Control-Request-Method" in self.headers:
            self.send_header("Access-Control-Allow-Methods", self.methods)
            requested = self.headers.get("Access-Control-Request-Headers")
            if requested is not None and requested.isprintable():
                self.send_header("Access-Control-Allow-Headers", requested)
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self) -> None:
        path = self.route
        if path.startswith("/_fake/"):
            self._fake_get(path)
            return
        if path in ("/", "/index.html", "/favicon.ico"):
            if not self.server.webui:
                self._safe_error(APIError(404, "not found", "not_found"))
            elif path == "/favicon.ico":
                self._send(200, b'<svg xmlns="http://www.w3.org/2000/svg"/>', "image/svg+xml")
            else:
                self._send(200, b"<!doctype html><title>Splash (fake)</title>", "text/html; charset=utf-8")
            return
        if path == "/health":
            self._json(200, {"status": "ok"})
            return
        if path == "/ready":
            ready = self.server.status()["ready"] is True
            self._json(200 if ready else 503, {"status": "ready" if ready else "unavailable"})
            return
        if path == "/status":
            self._json(200, self.server.status())
            return
        if path == "/metrics":
            self._send(
                200, prometheus_metrics(self.server.status()).encode(), "text/plain; version=0.0.4; charset=utf-8"
            )
            return
        match = re.fullmatch(r"/v1/responses/(resp_[A-Za-z0-9_]+)", path)
        if match:
            stored = self.engine.response_store.get(match.group(1))
            if stored is None:
                self._safe_error(APIError(404, "response not found", "not_found_error"))
            else:
                self._json(200, stored[0])
            return
        if path == "/v1/models" or path.startswith("/v1/models/"):
            self._models(path)
            return
        self._safe_error(APIError(404, "not found", "not_found"))

    def _models(self, path: str) -> None:
        engine = self.engine
        models = [
            {
                "id": name,
                "object": "model",
                "created": 0,
                "owned_by": "splash",
                "max_model_len": engine.max_context,
                "context_length": engine.max_context,
                "vision": engine.vision,
                "input_modalities": engine.input_modalities,
                **({"root": engine.response_model} if name != engine.response_model else {}),
            }
            for name in engine.model_names
        ]
        if path == "/v1/models":
            typed = [{"name": m["id"], "description": "Splash resident model", "release_date": ""} for m in models]
            self._json(200, {"object": "list", "data": models, "models": typed})
            return
        name = path.removeprefix("/v1/models/")
        model = next((m for m in models if m["id"] == name), None)
        if model is None:
            self._safe_error(APIError(404, "model not found", "model_not_found"))
        else:
            self._json(200, model)

    def do_DELETE(self) -> None:
        path = self.route
        if path == "/_fake/last_requests":
            self.server.recorder.clear()
            self._json(200, {"cleared": True})
            return
        match = re.fullmatch(r"/v1/responses/(resp_[A-Za-z0-9_]+)", path)
        if match is None:
            self._safe_error(APIError(404, "not found", "not_found"))
            return
        if not self.engine.response_store.delete(match.group(1)):
            self._safe_error(APIError(404, "response not found", "not_found_error"))
            return
        self._json(200, {"id": match.group(1), "object": "response", "deleted": True})

    # --- control endpoints -------------------------------------------------------
    def _fake_get(self, path: str) -> None:
        if path == "/_fake/state":
            self._json(200, self.server.fake_state())
        elif path == "/_fake/last_requests":
            query = parse_qs(urlsplit(self.path).query)
            n = int(query["n"][0]) if "n" in query else None
            self._json(200, {"requests": self.server.recorder.list(n)})
        else:
            self._json(404, {"error": "unknown fake endpoint"})

    def _fake_post(self, path: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if not isinstance(body, dict):
                raise APIError(400, "body must be an object")
            if path == "/_fake/mode":
                if "config" in body:
                    if not isinstance(body["config"], dict):
                        raise APIError(400, "config must be an object")
                    self.engine.config.update(body["config"])
                if "mode" in body:
                    self.engine.set_mode(body["mode"])
                self._json(200, self.server.fake_state())
            elif path == "/_fake/crash":
                self._json(200, {"crashing": True})
                self.wfile.flush()
                self.server.crash(body)
            else:
                self._json(404, {"error": "unknown fake endpoint"})
        except APIError as error:
            self._json(error.status, {"error": error.message})
        except (ValueError, TypeError) as error:
            self._json(400, {"error": str(error)})

    # --- POST ----------------------------------------------------------------------
    def do_POST(self) -> None:
        started_at = time.monotonic()
        path = self.route
        if path.startswith("/_fake/"):
            self._fake_post(path)
            return
        if path not in POST_ROUTES:
            self._safe_error(APIError(404, "not found", "not_found"))
            return
        count_tokens = path == "/v1/messages/count_tokens"
        prompt_only = count_tokens or path in ("/tokenize", "/apply-template")
        systemone = path == "/v1/systemone"
        engine = self.engine
        refusal = None if prompt_only else engine.refusal()
        if refusal is not None:
            if systemone and refusal.status == 503:
                refusal = APIError(529, refusal.message, refusal.code)
            self._safe_error(refusal, log=False)
            return
        admission = engine.token_counts if prompt_only else engine.requests
        if (not prompt_only and engine.queue_full()) or not admission.acquire():
            self._safe_error(
                APIError(529 if systemone else 503, "frontend request capacity is exhausted", "frontend_overloaded")
            )
            return
        try:
            with engine.latencies.measure("upload"):
                body = self._read_json_body()
            if not isinstance(body, dict):
                if systemone:
                    raise SystemOneError([detail([], "request body must be an object")])
                raise APIError(400, "request body must be an object")
            deadline = self._deadline(body, started_at)
            handler: Callable[[dict, float], None] = {
                "/v1/chat/completions": self._chat,
                "/v1/completions": self._completions,
                "/v1/responses": self._responses,
                "/v1/messages": self._messages,
                "/v1/messages/count_tokens": self._count_tokens,
                "/tokenize": self._tokenize,
                "/apply-template": self._apply_template,
                "/v1/judgments": self._judgment,
                "/v1/systemone": self._systemone,
            }[path]
            handler(body, deadline)
        except SystemOneError as error:
            self._systemone_error(error)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except APIError as error:
            if systemone and error.status == 503:
                error = APIError(529, error.message, error.code)
            self._safe_error(error)
        except Exception as error:  # pragma: no cover - diagnostics
            print_status(f"Error · internal_server_error · {type(error).__name__}", error=True)
            self._safe_error(APIError(500, "internal server error", "internal_server_error"), log=False)
        finally:
            admission.release()
            engine.latencies.observe("http_request", time.monotonic() - started_at)

    def _deadline(self, body: dict, started_at: float) -> float:
        timeout = body.get("timeout")
        server_timeout = self.engine.settings.request_timeout or math.inf
        if timeout is None:
            timeout = server_timeout
        elif not is_finite_number(timeout) or timeout <= 0:
            raise APIError(400, "timeout must be positive")
        return started_at + min(timeout, server_timeout)

    # --- request preparation (server/frontend.py) -------------------------------
    def _check_model(self, body: dict) -> None:
        if not self.engine.accepts_model(body.get("model", self.engine.settings.model)):
            raise APIError(404, f"model {body['model']} not found", "model_not_found")

    @staticmethod
    def _drop_nulls(body: dict, extras: tuple[str, ...]) -> dict:
        nullable = {*SAMPLING_FIELDS, *extras}
        return {k: v for k, v in body.items() if v is not None or k not in nullable}

    @staticmethod
    def _priority(body: dict) -> str:
        priority = body.get("priority", "normal")
        if not isinstance(priority, str) or priority not in PRIORITIES:
            raise APIError(400, "priority must be foreground, normal, or background")
        return priority

    def _generation_options(self, body: dict) -> dict:
        for name, (default, accepts, requirement) in SAMPLING_NUMBERS.items():
            value = body.get(name, default)
            if not is_finite_number(value) or not accepts(value):
                raise APIError(400, f"{name} must be {requirement}")
        top_k = body.get("top_k", 20)
        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < -1:
            raise APIError(400, "top_k must be 0 or -1 (disabled) or a positive integer")
        stop = body.get("stop")
        if stop in (None, []):
            stops: tuple[str, ...] = ()
        elif isinstance(stop, str) and stop:
            stops = (stop,)
        elif isinstance(stop, list) and 1 <= len(stop) <= 4 and all(isinstance(s, str) and s for s in stop):
            stops = tuple(stop)
        else:
            raise APIError(400, "stop must be a string or up to four strings")
        if body.get("logit_bias") not in (None, {}):
            raise APIError(400, "logit_bias is not supported")
        ignore_eos = body.get("ignore_eos", False)
        if not isinstance(ignore_eos, bool):
            raise APIError(400, "ignore_eos must be a boolean")
        seed = body.get("seed")
        if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2**64):
            raise APIError(400, "seed must be an unsigned 64-bit integer")
        return {"stops": stops, "ignore_eos": ignore_eos, "priority": self._priority(body)}

    def _output_budget(self, requested: object, prompt_tokens: list[int], field: str, clamp: bool) -> tuple[int, bool]:
        context = self.engine.max_context
        if len(prompt_tokens) >= context:
            raise ContextLengthError(len(prompt_tokens), context - 1)
        remaining = context - len(prompt_tokens)
        max_new = remaining if requested is None else requested
        if not isinstance(max_new, int) or isinstance(max_new, bool) or max_new <= 0:
            raise APIError(400, f"{field} must be a positive integer")
        clamped = False
        if max_new > remaining:
            if not clamp:
                raise APIError(
                    400,
                    f"prompt and {field} exceed the context window: "
                    f"{len(prompt_tokens)} + {max_new} > {context} tokens",
                    "context_length_exceeded",
                )
            max_new, clamped = remaining, True
        return max_new, clamped

    def _normalize_messages(self, messages: object) -> list[dict]:
        if not isinstance(messages, list) or not messages:
            raise APIError(400, "messages must be a non-empty array")
        for message in messages:
            if not isinstance(message, dict):
                raise APIError(400, "each message must be an object")
            if not self.engine.vision and (modality := self._media_modality(message.get("content"))):
                # server/api_shapes.py _require_vision
                raise APIError(400, f"{modality} input is not supported: {VISION_UNAVAILABLE}")
        if self.engine.config.template == "unsupported" and any(
            m.get("role") in ("system", "developer") for m in messages[1:]
        ):
            raise APIError(400, LATER_SYSTEM_UNSUPPORTED)
        return messages

    @staticmethod
    def _media_modality(content: object) -> str | None:
        """The modality of the first image or PDF part: every API shape's
        media parts become image_url or file parts (api_shapes.py)."""
        if not isinstance(content, list):
            return None
        for part in content:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind in ("image_url", "input_image", "image"):
                return "image"
            if kind in ("file", "input_file", "document"):
                return "PDF"
        return None

    def _thinking(self, body: dict) -> bool:
        effort = body.get("reasoning_effort")
        if effort is None:
            effort = self.engine.settings.default_reasoning_effort
        if effort is not None and (not isinstance(effort, str) or effort not in REASONING_EFFORTS):
            raise APIError(400, "invalid reasoning_effort")
        kwargs = body.get("chat_template_kwargs")
        if kwargs is None:
            kwargs = {}
        elif not isinstance(kwargs, dict):
            raise APIError(400, "chat_template_kwargs must be an object")
        elif reserved := sorted(RESERVED_TEMPLATE_KWARGS & kwargs.keys()):
            raise APIError(400, f"chat_template_kwargs cannot set {reserved[0]}")
        if kwargs.get("enable_thinking") is False:
            return False
        return effort != "none"

    def _chat_job(
        self, body: dict, *, output_field: str | None, clamp: bool, thinking_display: str = "summarized"
    ) -> FakeJob:
        """frontend.py Frontend._prepare for a Chat-shaped body."""
        body = self._drop_nulls(body, ("n", "max_tokens", "max_completion_tokens", "stream", "parallel_tool_calls"))
        options = self._generation_options(body)
        n, logprobs = body.get("n", 1), body.get("logprobs")
        if (
            not isinstance(n, int)
            or isinstance(n, bool)
            or n != 1
            or (logprobs is not None and (not isinstance(logprobs, bool) or logprobs))
        ):
            raise APIError(400, "n and logprobs are not currently supported")
        self._check_model(body)
        thinking = self._thinking(body)
        messages = self._normalize_messages(body.get("messages"))
        tools = body.get("tools")
        if tools is not None and not isinstance(tools, list):
            raise APIError(400, "tools must be an array")
        response_format = body.get("response_format")
        structured = isinstance(response_format, dict) and response_format.get("type") in ("json_object", "json_schema")
        if options["stops"] and (tools or structured):
            raise APIError(400, "stop cannot be combined with tools or structured output")
        if options["ignore_eos"] and (tools or structured):
            raise APIError(400, "ignore_eos cannot be combined with tools or structured output")
        with self.engine.latencies.measure("template"):
            rendered = fake_text.render_chat(messages, tools=tools, thinking=thinking)
        requested = body.get("max_completion_tokens", body.get("max_tokens"))
        if output_field is None:
            output_field = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
        max_new, clamped = self._output_budget(requested, rendered.tokens, output_field, clamp)
        return FakeJob(
            public_id=secrets.token_hex(16),
            prompt_tokens=rendered.tokens,
            prompt_text=rendered.text,
            max_new_tokens=max_new,
            thinking=thinking,
            thinking_display=thinking_display,
            stop_sequences=options["stops"],
            ignore_eos=options["ignore_eos"],
            tools=tools or None,
            tool_choice=body.get("tool_choice", "auto"),
            parallel_tool_calls=body.get("parallel_tool_calls", True) is not False,
            response_format=response_format,
            output_clamped_to_context=clamped,
            priority=options["priority"],
            last_user=rendered.last_user,
        )

    def _streaming_flags(self, body: dict, *, anthropic: bool = False) -> tuple[bool, bool]:
        stream = body.get("stream", False)
        if stream is None and not anthropic:
            stream = False
        return_progress = body.get("return_progress", False)
        if not isinstance(return_progress, bool) or (return_progress and stream is not True):
            raise APIError(400, "return_progress requires stream: true and must be a boolean")
        return stream is True, return_progress

    def _stream_options(self, body: dict, stream: object) -> dict:
        options = body.get("stream_options")
        if options is None:
            options = {}
        if (
            not isinstance(body.get("stream", False) or False, bool)
            or not isinstance(options, dict)
            or not isinstance(options.get("include_usage", False), bool)
        ):
            raise APIError(400, "invalid streaming options")
        return {"include_usage": options.get("include_usage", False)}

    def _run(self, job: FakeJob, deadline: float) -> Iterator[tuple]:
        """The engine's events, cancelled when the client goes away."""
        job.deadline = deadline
        events = self.engine.generate(job)
        try:
            for event in events:
                if self._client_disconnected():
                    raise ConnectionResetError
                yield event
        finally:
            events.close()

    def _collect(
        self,
        job: FakeJob,
        deadline: float,
        *,
        on_start=None,
        on_text=None,
        on_tool=None,
        on_idle=None,
        on_progress=None,
    ) -> tuple[str, str, list[dict], NativeResult]:
        reasoning: list[str] = []
        content: list[str] = []
        calls: list[dict] = []
        result: NativeResult | None = None
        for event in self._run(job, deadline):
            kind = event[0]
            if kind == "idle" and on_idle:
                on_idle()
            elif kind == "start" and on_start:
                on_start()
            elif kind == "progress" and on_progress:
                on_progress(event[1])
            elif kind == "text":
                (reasoning if event[1] == "reasoning_content" else content).append(event[2])
                if on_text:
                    on_text(event[1], event[2])
            elif kind == "tool":
                _, call_id, name, parts = event
                calls.append(
                    {"id": call_id, "type": "function", "function": {"name": name, "arguments": "".join(parts)}}
                )
                if on_tool:
                    on_tool(call_id, name, parts, len(calls) - 1)
            elif kind == "done":
                result = event[1]
        if result is None:  # the engine always ends with "done" or raises
            raise APIError(500, "internal server error", "internal_server_error")
        return "".join(reasoning), "".join(content), calls, result

    def _guarded(self, run: Callable[[], None], send_error: Callable[[APIError], None]) -> None:
        """server/server.py _guarded_stream."""
        try:
            run()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except APIError as error:
            if not self._response_started:
                raise
            if error.status >= 500:
                self._log_api_error(error)
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, TimeoutError):
                send_error(error)

    # --- Chat and Completions ----------------------------------------------------
    def _chat(self, body: dict, deadline: float) -> None:
        stream, progress = self._streaming_flags(body)
        stream_options = self._stream_options(body, stream)
        job = self._chat_job(body, output_field=None, clamp=False)
        job.return_progress = progress
        if stream:
            self._openai_stream(job, deadline, stream_options, chat=True)
            return
        reasoning, content, calls, result = self._collect(job, deadline)
        message: dict = {"role": "assistant", "content": content or None}
        if reasoning:
            message["reasoning_content"] = reasoning
        if calls:
            message["tool_calls"] = calls
        self._json(200, completion_response(self.engine.response_model, job, result, message, bool(calls)))

    def _completions(self, body: dict, deadline: float) -> None:
        stream, progress = self._streaming_flags(body)
        stream_options = self._stream_options(body, stream)
        body = self._drop_nulls(body, ("n", "best_of", "max_tokens", "suffix", "echo", "logprobs"))
        self._check_model(body)
        for name in ("suffix", "logprobs"):
            if name in body:
                raise APIError(400, f"{name} is not supported")
        if body.get("echo", False) is not False:
            raise APIError(400, "echo is not supported")
        for name in ("best_of", "n"):
            value = body.get(name, 1)
            if not isinstance(value, int) or isinstance(value, bool) or value != 1:
                raise APIError(400, f"{name} must be 1")
        options = self._generation_options(body)
        prompt = body.get("prompt")
        if isinstance(prompt, str):
            tokens, text = fake_text.tokenize(prompt), prompt
        elif isinstance(prompt, list) and all(type(t) is int for t in prompt):
            if any(not 0 <= t < fake_text.VOCAB_SIZE for t in prompt):
                raise APIError(400, "prompt token ids must be in the vocabulary")
            tokens, text = list(prompt), " ".join(map(str, prompt))
        else:
            raise APIError(400, "prompt must be one string or one array of token ids")
        if not tokens:
            raise APIError(400, "prompt must not be empty")
        requested = body.get("max_tokens")
        max_new, _ = self._output_budget(
            COMPLETION_DEFAULT_MAX_TOKENS if requested is None else requested, tokens, "max_tokens", requested is None
        )
        job = FakeJob(
            public_id=secrets.token_hex(16),
            prompt_tokens=tokens,
            prompt_text=text,
            max_new_tokens=max_new,
            stop_sequences=options["stops"],
            ignore_eos=options["ignore_eos"],
            priority=options["priority"],
            last_user=text,
            return_progress=progress,
        )
        if stream:
            self._openai_stream(job, deadline, stream_options, chat=False)
            return
        _, content, _, result = self._collect(job, deadline)
        self._json(200, text_completion_response(self.engine.response_model, job, result, content))

    def _openai_stream(self, job: FakeJob, deadline: float, stream_options: dict, *, chat: bool) -> None:
        chunk = partial(
            stream_chunk if chat else text_completion_chunk, self.engine.response_model, job.public_id, job.created_at
        )
        empty: object = {} if chat else ""
        started = False

        def payload(field_name: str, text: str) -> object:
            return {field_name: text} if chat else text

        def start() -> None:
            nonlocal started
            started = True
            self._start_event_stream()
            if chat:
                self._sse(chunk({"role": "assistant", "content": ""}))

        def keepalive() -> None:
            if started:
                self._sse(chunk(empty))
            else:
                self._sse_keepalive()

        def tool(call_id: str, name: str, parts: list[str], index: int) -> None:
            self._sse(
                chunk({"tool_calls": [{"index": index, "id": call_id, "type": "function", "function": {"name": name}}]})
            )
            for part in parts:
                self._sse(chunk({"tool_calls": [{"index": index, "function": {"arguments": part}}]}))

        def run() -> None:
            _, _, calls, result = self._collect(
                job,
                deadline,
                on_start=start,
                on_text=lambda field_name, text: self._sse(chunk(payload(field_name, text))),
                on_tool=tool if chat else None,
                on_idle=keepalive,
                on_progress=lambda progress: self._sse(chunk(empty) | {"prompt_progress": progress}),
            )
            self._sse(chunk(empty, finish_reason(result, calls), timings=timings_dict(result)))
            if stream_options.get("include_usage"):
                self._sse(chunk(empty, usage=usage_dict(result, job), metrics=result.metrics))
            self._sse("[DONE]")

        self._guarded(run, self._sse_error)

    # --- Responses -------------------------------------------------------------------
    def _responses(self, body: dict, deadline: float) -> None:
        stream, progress = self._streaming_flags(body)
        if body.get("stream") is not None and not isinstance(body["stream"], bool):
            raise APIError(400, "stream must be a boolean")
        store = body.get("store")
        if store is not None and not isinstance(store, bool):
            raise APIError(400, "store must be a boolean")
        store = True if store is None else store
        previous_id = body.get("previous_response_id")
        if previous_id is not None and (not isinstance(previous_id, str) or not previous_id):
            raise APIError(400, "previous_response_id must be a non-empty string")
        if body.get("conversation") is not None:
            raise APIError(400, "conversation is not supported")
        if body.get("background") not in (None, False):
            raise APIError(400, "background responses are not supported")
        if body.get("truncation") not in (None, "disabled"):
            raise APIError(400, "only disabled truncation is supported")
        previous_items: list = []
        if previous_id is not None:
            stored = self.engine.response_store.get(previous_id)
            if stored is None:
                raise APIError(404, "previous response not found", "previous_response_not_found")
            previous_items = stored[1]
        items = [*previous_items, *self._canonical_input(body.get("input"))]
        instructions = body.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise APIError(400, "instructions must be a string")
        chat: dict = {"messages": self._responses_messages(instructions, items)}
        if body.get("tools") is not None:
            chat["tools"] = [
                {"type": "function", "function": {k: v for k, v in t.items() if k != "type"}}
                if isinstance(t, dict) and "function" not in t
                else t
                for t in body["tools"]
            ]
        choice = body.get("tool_choice")
        if isinstance(choice, dict):
            choice = {"type": "function", "function": {"name": choice.get("name")}}
        if choice is not None:
            chat["tool_choice"] = choice
        reasoning = body.get("reasoning")
        if reasoning is not None and not isinstance(reasoning, dict):
            raise APIError(400, "reasoning must be an object")
        if reasoning and reasoning.get("effort") is not None:
            chat["reasoning_effort"] = reasoning["effort"]
        text = body.get("text")
        if (
            isinstance(text, dict)
            and isinstance(text.get("format"), dict)
            and text["format"].get("type") == "json_schema"
        ):
            fmt = text["format"]
            chat["response_format"] = {
                "type": "json_schema",
                "json_schema": {k: v for k, v in fmt.items() if k != "type"},
            }
        for name in (
            "model",
            *SAMPLING_FIELDS,
            "logit_bias",
            "seed",
            "timeout",
            "priority",
            "stop",
            "parallel_tool_calls",
        ):
            if body.get(name) is not None:
                chat[name] = body[name]
        if body.get("max_output_tokens") is not None:
            chat["max_completion_tokens"] = body["max_output_tokens"]
        job = self._chat_job(chat, output_field="max_output_tokens", clamp=False)
        job.response_store = store
        job.response_previous_id = previous_id
        job.response_history_items = items if store else None
        job.return_progress = progress
        if stream:
            self._responses_stream(job, deadline)
            return
        sequencer = BlockSequencer()
        _, _, _, result = self._collect(
            job,
            deadline,
            on_text=sequencer.text,
            on_tool=lambda call_id, name, parts, index: sequencer.tool(call_id, name, parts),
        )
        incomplete = result.reason == "length"
        output = responses_output(job, sequencer.finish(incomplete))
        response = responses_response(
            self.engine.response_model, job, "incomplete" if incomplete else "completed", output, result=result
        )
        self._persist(job, response, output)
        self._json(200, response)

    @staticmethod
    def _canonical_input(value: object) -> list:
        if isinstance(value, str) and value:
            return [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": value}]}]
        if isinstance(value, list) and value:
            items = []
            for item in value:
                if not isinstance(item, dict):
                    raise APIError(400, "each input item must be an object")
                items.append(item if "type" in item else {"type": "message", **item})
            return items
        raise APIError(400, "input must be a non-empty string or array")

    @staticmethod
    def _responses_messages(instructions: str | None, items: list) -> list[dict]:
        messages: list[dict] = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        for item in items:
            kind = item.get("type")
            if kind == "message":
                messages.append(
                    {"role": item.get("role", "user"), "content": fake_text.content_text(item.get("content"))}
                )
            elif kind == "function_call":
                messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": item.get("call_id"),
                                "type": "function",
                                "function": {"name": item.get("name"), "arguments": item.get("arguments", "{}")},
                            }
                        ],
                    }
                )
            elif kind == "function_call_output":
                messages.append({"role": "tool", "content": fake_text.content_text(item.get("output"))})
        if not messages:
            raise APIError(400, "input must be a non-empty string or array")
        return messages

    def _persist(self, job: FakeJob, response: dict, output: list) -> None:
        if not job.response_store:
            return
        if not self.engine.response_store.put(response, [*(job.response_history_items or []), *output]):
            response["store"] = False

    def _responses_stream(self, job: FakeJob, deadline: float) -> None:
        output: list = []
        sequence = 0
        model = self.engine.response_model

        def send(event: str, **payload: object) -> None:
            nonlocal sequence
            self._event_sse(event, {"type": event, "sequence_number": sequence, **payload})
            sequence += 1

        def begin() -> None:
            if self._response_started:
                return
            self._start_event_stream()
            send("response.created", response=responses_response(model, job, "in_progress", []))
            send("response.in_progress", response=responses_response(model, job, "in_progress", []))

        def open_item(index, block) -> None:
            item = responses_item(job, block, index)
            send("response.output_item.added", output_index=index, item=item)
            if block.kind == "reasoning":
                send(
                    "response.reasoning_summary_part.added",
                    item_id=item["id"],
                    output_index=index,
                    summary_index=0,
                    part={"type": "summary_text", "text": ""},
                )
            elif block.kind == "text":
                send(
                    "response.content_part.added",
                    item_id=item["id"],
                    output_index=index,
                    content_index=0,
                    part={"type": "output_text", "text": "", "annotations": []},
                )

        def item_delta(index, block, text) -> None:
            extra: dict[str, Any]
            if block.kind == "tool":
                event, extra = "response.function_call_arguments.delta", {}
            elif block.kind == "reasoning":
                event, extra = "response.reasoning_summary_text.delta", {"summary_index": 0}
            else:
                event, extra = "response.output_text.delta", {"content_index": 0, "logprobs": []}
            send(event, item_id=responses_item_id(job, block.kind, index), output_index=index, delta=text, **extra)

        def close_item(index, block) -> None:
            item = responses_item(job, block, index)
            if block.kind == "tool":
                send(
                    "response.function_call_arguments.done",
                    item_id=item["id"],
                    output_index=index,
                    name=item["name"],
                    arguments=item["arguments"],
                )
            elif block.kind == "reasoning":
                send(
                    "response.reasoning_summary_text.done",
                    item_id=item["id"],
                    output_index=index,
                    summary_index=0,
                    text=block.text,
                )
                payload: dict = {
                    "item_id": item["id"],
                    "output_index": index,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": block.text},
                }
                if block.status == "incomplete":
                    payload["status"] = "incomplete"
                send("response.reasoning_summary_part.done", **payload)
            else:
                part = item["content"][0]
                send(
                    "response.output_text.done",
                    item_id=item["id"],
                    output_index=index,
                    content_index=0,
                    text=part["text"],
                    logprobs=[],
                )
                send("response.content_part.done", item_id=item["id"], output_index=index, content_index=0, part=part)
            send("response.output_item.done", output_index=index, item=item)
            output.append(item)

        sequencer = BlockSequencer(open_item, item_delta, close_item)

        def keepalive() -> None:
            begin()
            if not sequencer.blocks:
                send("response.in_progress", response=responses_response(model, job, "in_progress", []))
            else:
                self._sse_keepalive()

        def run() -> None:
            _, _, _, result = self._collect(
                job,
                deadline,
                on_start=begin,
                on_text=sequencer.text,
                on_tool=lambda call_id, name, parts, index: sequencer.tool(call_id, name, parts),
                on_idle=keepalive,
                on_progress=lambda progress: send(
                    "response.in_progress",
                    response=responses_response(model, job, "in_progress", []),
                    prompt_progress=progress,
                ),
            )
            incomplete = result.reason == "length"
            sequencer.finish(incomplete)
            status = "incomplete" if incomplete else "completed"
            response = responses_response(model, job, status, output, result=result)
            self._persist(job, response, output)
            send(f"response.{status}", response=response)

        def send_error(error: APIError) -> None:
            send(
                "response.failed",
                response=responses_response(
                    model,
                    job,
                    "failed",
                    output,
                    error={"type": error.protocol_type(), "code": error.code, "message": error.message},
                ),
            )

        self._guarded(run, send_error)

    # --- Anthropic Messages ----------------------------------------------------------
    def _anthropic_chat(self, body: dict) -> tuple[dict, str]:
        """api_shapes.py _anthropic_chat: the prompt as a Chat body."""
        if not isinstance(body.get("model"), str) or not body["model"]:
            raise APIError(400, "model must be a non-empty string")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise APIError(400, "messages must be a non-empty array")
        thinking = body.get("thinking")
        output_config = body.get("output_config", {})
        if not isinstance(output_config, dict):
            raise APIError(400, "output_config must be an object")
        effort = output_config.get("effort", "high")
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            raise APIError(400, "output_config.effort is invalid")
        display = "summarized"
        if thinking is None:
            effort = "none"
        else:
            if not isinstance(thinking, dict) or thinking.get("type") not in ("enabled", "disabled", "adaptive"):
                raise APIError(400, "thinking.type must be enabled, disabled, or adaptive")
            if thinking["type"] == "disabled":
                effort = "none"
            if thinking.get("display") in ("omitted", "updates"):
                display = "omitted"
        translated: list[dict] = []
        system = body.get("system")
        if system is not None:
            translated.append({"role": "system", "content": fake_text.content_text(system)})
        for message in messages:
            if not isinstance(message, dict):
                raise APIError(400, "Anthropic messages must be objects")
            role = message.get("role")
            if role not in ("user", "assistant", "system"):
                raise APIError(400, "Anthropic message role must be user, assistant, or system")
            translated.append({"role": role, "content": message.get("content")})
        chat: dict = {"model": body["model"], "messages": translated, "reasoning_effort": effort}
        tools = body.get("tools")
        if tools is not None:
            chat["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name"),
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
                for t in tools
                if isinstance(t, dict)
            ]
        choice = body.get("tool_choice")
        if isinstance(choice, dict):
            kind = choice.get("type")
            chat["tool_choice"] = (
                {"type": "function", "function": {"name": choice.get("name")}}
                if kind == "tool"
                else {"any": "required", "auto": "auto", "none": "none"}.get(str(kind), "auto")
            )
        return chat, display

    def _messages(self, body: dict, deadline: float) -> None:
        max_tokens = body.get("max_tokens")
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
            raise APIError(400, "max_tokens must be a positive integer")
        if not isinstance(body.get("stream", False), bool):
            raise APIError(400, "stream must be a boolean")
        stream, progress = self._streaming_flags(body, anthropic=True)
        chat, display = self._anthropic_chat(body)
        if body["messages"][-1].get("role") == "assistant":
            raise APIError(400, "a final assistant message (prefill) is not supported; end messages with a user turn")
        chat.update(max_completion_tokens=max_tokens)
        for source, target in (
            ("stop_sequences", "stop"),
            ("temperature", "temperature"),
            ("top_p", "top_p"),
            ("top_k", "top_k"),
            ("seed", "seed"),
            ("timeout", "timeout"),
            ("priority", "priority"),
        ):
            if body.get(source) is not None:
                chat[target] = body[source]
        job = self._chat_job(chat, output_field="max_tokens", clamp=True, thinking_display=display)
        job.return_progress = progress
        if stream:
            self._anthropic_stream(job, deadline)
            return
        sequencer = BlockSequencer()
        reasoning, _, calls, result = self._collect(
            job,
            deadline,
            on_text=sequencer.text,
            on_tool=lambda call_id, name, parts, index: sequencer.tool(call_id, name, parts),
        )
        blocks = sequencer.finish(result.reason == "length")
        signature = self._signature(reasoning) if reasoning and job.thinking_display == "omitted" else ""
        self._json(200, anthropic_response(self.engine.response_model, job, blocks, result, calls, signature))

    @staticmethod
    def _signature(text: str) -> str:
        return base64.b64encode(b"fake-splash-thinking:" + hashlib.sha256(text.encode()).digest()).decode()

    def _anthropic_stream(self, job: FakeJob, deadline: float) -> None:
        omitted = job.thinking_display == "omitted"
        model = self.engine.response_model
        state = {"cached": 0}

        def send(event: str, payload: dict) -> None:
            self._event_sse(event, {"type": event, **payload})

        def start() -> None:
            self._start_event_stream()
            send(
                "message_start",
                {
                    "message": {
                        "id": f"msg_{job.public_id}",
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": anthropic_usage(len(job.prompt_tokens), 0, _Cache(state["cached"])),
                    }
                },
            )

        def keepalive() -> None:
            self._start_event_stream()
            send("ping", {})

        def open_block(index, block) -> None:
            if block.kind == "reasoning":
                content_block: dict = {"type": "thinking", "thinking": "", "signature": ""}
            elif block.kind == "text":
                content_block = {"type": "text", "text": ""}
            else:
                content_block = {"type": "tool_use", "id": block.call_id, "name": block.name, "input": {}}
            send("content_block_start", {"index": index, "content_block": content_block})
            if block.kind == "reasoning" and omitted:
                send("content_block_delta", {"index": index, "delta": {"type": "thinking_delta", "thinking": ""}})

        def block_delta(index, block, text) -> None:
            if block.kind == "reasoning":
                if omitted:
                    return
                delta = {"type": "thinking_delta", "thinking": text}
            elif block.kind == "text":
                delta = {"type": "text_delta", "text": text}
            else:
                delta = {"type": "input_json_delta", "partial_json": text}
            send("content_block_delta", {"index": index, "delta": delta})

        def close_block(index, block) -> None:
            if block.kind == "reasoning" and omitted:
                send(
                    "content_block_delta",
                    {"index": index, "delta": {"type": "signature_delta", "signature": self._signature(block.text)}},
                )
            send("content_block_stop", {"index": index})

        sequencer = BlockSequencer(open_block, block_delta, close_block)

        def run() -> None:
            events = self._run(job, deadline)
            calls: list = []
            result: NativeResult | None = None
            for event in events:
                kind = event[0]
                if kind == "idle":
                    keepalive()
                elif kind == "start":
                    state["cached"] = event[1]
                    start()
                elif kind == "progress":
                    send("ping", {"prompt_progress": event[1]})
                elif kind == "text":
                    sequencer.text(event[1], event[2])
                elif kind == "tool":
                    calls.append(event)
                    sequencer.tool(event[1], event[2], event[3])
                elif kind == "done":
                    result = event[1]
            if result is None:
                raise APIError(500, "internal server error", "internal_server_error")
            sequencer.finish(result.reason == "length")
            send(
                "message_delta",
                {
                    "delta": {
                        "stop_reason": anthropic_stop(result, calls, job.output_clamped_to_context),
                        "stop_sequence": result.stop_sequence,
                    },
                    "usage": {"output_tokens": result.completion_tokens},
                },
            )
            send("message_stop", {})

        def send_error(error: APIError) -> None:
            send("error", {"error": {"type": error.protocol_type(True), "message": error.message}})

        self._guarded(run, send_error)

    # --- prompt-only endpoints -----------------------------------------------------
    def _count_tokens(self, body: dict, deadline: float) -> None:
        chat, _ = self._anthropic_chat(body)
        self._check_model(chat)
        thinking = self._thinking(chat)
        messages = self._normalize_messages(chat["messages"])
        rendered = fake_text.render_chat(messages, tools=chat.get("tools"), thinking=thinking)
        self._json(200, {"input_tokens": len(rendered.tokens)})

    def _tokenize(self, body: dict, deadline: float) -> None:
        content = body.get("content")
        if not isinstance(content, str):
            raise APIError(400, "content must be a string")
        add_special = body.get("add_special", False)
        if not isinstance(add_special, bool):
            raise APIError(400, "add_special must be a boolean")
        for option, supported in (("parse_special", True), ("with_pieces", False)):
            if body.get(option, supported) is not supported:
                raise APIError(400, f"only {option}={str(supported).lower()} is supported")
        self._json(200, {"tokens": fake_text.tokenize(content)})

    def _apply_template(self, body: dict, deadline: float) -> None:
        add_generation_prompt = body.get("add_generation_prompt", True)
        if not isinstance(add_generation_prompt, bool):
            raise APIError(400, "add_generation_prompt must be a boolean")
        self._check_model(body)
        thinking = self._thinking(body)
        messages = self._normalize_messages(body.get("messages"))
        rendered = fake_text.render_chat(
            messages, tools=body.get("tools"), thinking=thinking, add_generation_prompt=add_generation_prompt
        )
        self._json(200, {"prompt": rendered.text})

    # --- scoring ---------------------------------------------------------------------
    def _score(self, prompt: str, options: int, deadline: float, priority: str):
        tokens = fake_text.tokenize(prompt)
        if len(tokens) > self.engine.max_context:
            raise ContextLengthError(len(tokens), self.engine.max_context)
        job = FakeJob(
            public_id=secrets.token_hex(16),
            prompt_tokens=tokens,
            prompt_text=prompt,
            max_new_tokens=0,
            priority=priority,
            deadline=deadline,
        )
        result = None
        for event in self.engine.score(job, options):
            if event[0] == "done":
                result = event[1]
        return job, result

    def _judgment(self, body: dict, deadline: float) -> None:
        unknown = sorted(set(body) - {"id", "state", "question", "options", "model", "timeout", "priority"})
        if unknown:
            raise APIError(400, f"unsupported fields: {', '.join(unknown)}")
        self._check_model(body)
        required = {"id", "state", "question", "options"}
        if not required <= body.keys():
            raise APIError(400, f"Row is missing fields: {sorted(required - body.keys())}")
        if not all(isinstance(body[k], str) and body[k] for k in ("id", "question")):
            raise APIError(400, "id and question must be nonempty strings")
        if not isinstance(body["state"], (str, dict, list)) or not body["state"]:
            raise APIError(400, "state must be a nonempty string, object, or array")
        options = body["options"]
        if not isinstance(options, list) or not 2 <= len(options) <= len(LETTERS):
            raise APIError(400, "options must contain 2-16 entries")
        if any(
            not isinstance(o, dict) or not isinstance(o.get("id"), str) or not isinstance(o.get("description"), str)
            for o in options
        ):
            raise APIError(400, "Each option needs string id and description fields")
        if len({o["id"] for o in options}) != len(options):
            raise APIError(400, "Option IDs must be unique")
        priority = self._priority(body)
        payload = {
            "evidence": body["state"],
            "criterion": body["question"],
            "options": [{"letter": LETTERS[i], "description": o["description"]} for i, o in enumerate(options)],
        }
        prompt = fake_text.render_chat(
            [
                {"role": "system", "content": DIRECT_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            thinking=False,
        ).text
        _, result = self._score(prompt, len(options), deadline, priority)
        logits = list(result.option_logits)
        self._json(
            200,
            {
                "id": body["id"],
                "option_ids": [o["id"] for o in options],
                "probabilities": softmax(logits),
                "option_logits": logits,
                "input_tokens": result.prompt_tokens,
                "answer_token_ids": [fake_text.token_id(letter) for letter in LETTERS[: len(options)]],
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "prompt_version": PROMPT_VERSION,
                "model": {"id": self.engine.response_model},
                "readout": READOUT,
                "probability_status": PROBABILITY_STATUS,
                "forward_seconds": result.start_to_first_token_ms / 1000.0,
                "total_seconds": result.request_wall_ms / 1000.0,
                "usage": {
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": 0,
                    "total_tokens": result.prompt_tokens,
                },
            },
        )

    def _systemone(self, body: dict, deadline: float) -> None:
        details: list[dict] = []
        model = body.get("model")
        if not isinstance(model, str) or not model:
            details.append(detail(["model"], "field required", "missing"))
        elif not self.engine.accepts_model(model):
            details.append(detail(["model"], f"model {model} is not served by this endpoint"))
        if "state" not in body:
            details.append(detail(["state"], "field required", "missing"))
        elif not isinstance(body["state"], (str, dict, list)):
            details.append(detail(["state"], "state must be a string, object, or array"))
        questions = body.get("questions")
        specs: list[tuple[str, str, list[str], object]] = []
        if questions is None:
            details.append(detail(["questions"], "field required", "missing"))
        elif not isinstance(questions, dict) or not questions:
            details.append(detail(["questions"], "questions must be a nonempty object"))
        else:
            for qid, question in questions.items():
                loc = ["questions", qid]
                if not isinstance(question, dict):
                    details.append(detail(loc, "question must be an object", "model_type"))
                    continue
                kind = question.get("type")
                criteria = question.get("criteria")
                if kind == "noul":
                    specs.append((qid, kind, ["true", "false"], None))
                elif kind == "choice":
                    if not isinstance(criteria, dict) or not criteria:
                        details.append(
                            detail(
                                [*loc, "criteria"],
                                "choice criteria must be a nonempty object mapping labels to descriptions",
                            )
                        )
                    else:
                        specs.append((qid, kind, list(criteria), None))
                elif kind == "score":
                    if not isinstance(criteria, list) or not criteria:
                        details.append(
                            detail([*loc, "criteria"], "score criteria must be a nonempty array of level descriptions")
                        )
                    else:
                        specs.append(
                            (
                                qid,
                                kind,
                                [str(i) for i in range(len(criteria))],
                                {str(i): v for i, v in enumerate(criteria)},
                            )
                        )
                else:
                    details.append(detail([*loc, "type"], "type must be noul, choice, or score"))
        try:
            priority = self._priority(body)
        except APIError as error:
            details.append(detail(["priority"], error.message))
            priority = "normal"
        if details:
            raise SystemOneError(details)
        answers: dict = {}
        input_tokens = 0
        for qid, kind, labels, legend in specs:
            if len(labels) == 1:
                probabilities = [1.0]
            else:
                prompt = json.dumps({"state": body["state"], "question": qid, "labels": labels})
                _, result = self._score(prompt, len(labels), deadline, priority)
                input_tokens += result.prompt_tokens
                probabilities = softmax(list(result.option_logits))
            if kind == "noul":
                answers[qid] = {"type": "noul", "noul": probabilities[0]}
            elif kind == "choice":
                best = max(range(len(probabilities)), key=probabilities.__getitem__)
                answers[qid] = {
                    "type": "choice",
                    "choice": labels[best],
                    "probabilities": dict(zip(labels, probabilities, strict=True)),
                    "confidence": concentration(probabilities),
                }
            else:
                answers[qid] = {
                    "type": "score",
                    "score": sum(i * p for i, p in enumerate(probabilities)),
                    "legend": legend,
                    "probabilities": {str(i): p for i, p in enumerate(probabilities)},
                    "confidence": concentration(probabilities),
                }
        self._json(
            200,
            {
                "model": self.engine.response_model,
                "answers": answers,
                "usage": {"input_tokens": input_tokens, "output_tokens": 0},
            },
        )

    def _systemone_error(self, error: SystemOneError) -> None:
        if self._response_started:
            return
        self._log_api_error(APIError(422, error.details[0]["msg"], "unprocessable_entity"))
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, TimeoutError):
            self._json(422, {"detail": error.details})


class _Cache:
    def __init__(self, matched: int):
        self.matched_tokens = matched


class FakeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def server_bind(self) -> None:
        """HTTPServer.server_bind without its `socket.getfqdn(host)`: that reverse
        DNS lookup goes through mDNS and can hang for minutes (GitHub's macOS runners)."""
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def __init__(
        self,
        address: tuple[str, int],
        engine: FakeEngine,
        *,
        api_key: str | None,
        allowed_hosts: list[str],
        allowed_origins: list,
        webui: bool,
        argv: list[str],
    ):
        self.engine = engine
        self.api_key = api_key
        self.webui = webui
        self.argv = argv
        self.recorder = Recorder()
        self.allowed_hosts = {
            h.lower().rstrip(".")
            for h in (*allowed_hosts, address[0], "localhost", "127.0.0.1", "::1")
            if h not in ("0.0.0.0", "::")  # noqa: S104 - server.py FrontendServer
        }
        self.allowed_origins = frozenset(allowed_origins)
        super().__init__(address, FakeHandler, bind_and_activate=False)

    def status(self) -> dict:
        return self.engine.status({"host": self.server_address[0], "port": self.server_address[1]})

    def fake_state(self) -> dict:
        engine = self.engine
        env_keys = (
            "TMPDIR",
            "HF_HUB_CACHE",
            "HF_HUB_OFFLINE",
            "HF_ENDPOINT",
            "SPLASH_CRASH_TRACE",
            "SPLASH_GUI_FAKE_DATA",
            "SPLASH_DEFAULT_REASONING_EFFORT",
        )
        return {
            "mode": engine.mode,
            "ready": engine.ready,
            "closing": engine.closing,
            "config": dict(vars(engine.config)),
            "settings": {
                k: (str(v) if v is not None and not isinstance(v, (int, float, str, bool, list)) else v)
                for k, v in vars(engine.settings).items()
            },
            "max_context": engine.max_context,
            "counters": {k: v for k, v in engine.c.items() if k != "oldest_wait_since"},
            "argv": self.argv,
            "env": {k: os.environ.get(k) for k in env_keys},
            "api_key_set": self.api_key is not None,
            "hf_token_set": bool(os.environ.get("HF_TOKEN")),
            "pid": os.getpid(),
        }

    def crash(self, body: dict) -> None:
        name = body.get("signal")
        write_stderr_line("error: native transport stopped after an engine failure (fake crash)")
        sys.stdout.flush()
        if isinstance(name, str) and hasattr(signal, name):
            os.kill(os.getpid(), getattr(signal, name))
            time.sleep(1)
        os._exit(int(body.get("code", self.engine.config.crash_code)))

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class _Interrupt(KeyboardInterrupt):
    pass


def _interrupt(_signum: int, _frame: object) -> None:
    raise _Interrupt


def settings_from_args(args) -> ServeSettings:
    return ServeSettings(
        model=args.model,
        host=args.host,
        port=args.port,
        max_context=args.max_context,
        max_memory=args.max_memory,
        max_cache_disk=args.max_cache_disk or 0,
        persistent_cache=args.persistent_cache,
        cache_dir=args.cache_dir,
        kv_format=args.kv_format,
        language_only=args.language_only,
        queue_size=args.queue_size,
        max_request_size=args.max_request_size,
        request_timeout=args.request_timeout,
        served_model_names=list(args.served_model_name),
        announce_served_name=args.announce_served_name,
        default_reasoning_effort=args.default_reasoning_effort,
        decode_share=args.decode_share,
        max_image_pixels=args.max_image_pixels,
    )


def serve(args, argv: list[str]) -> int:
    """server/server.py main(), over the fake engine. Returns the exit code."""
    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGINT, _interrupt)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, (signal.SIGINT, signal.SIGTERM))
    config = FakeConfig.from_env()
    engine = FakeEngine(settings_from_args(args), config)
    server = None
    exit_code = 0
    try:
        server = FakeServer(
            (args.host, args.port),
            engine,
            api_key=args.api_key,
            allowed_hosts=args.allowed_host,
            allowed_origins=args.allowed_origin,
            webui=not args.no_webui,
            argv=argv,
        )
        server.server_bind()
        if ANY_ORIGIN in args.allowed_origin and args.api_key is None:
            print_status(
                "Warning · --allowed-origin '*' without --api-key lets every web "
                "page open in a browser that reaches this server use it",
                error=True,
            )
        if config.listen_early:
            server.server_activate()
            threading.Thread(target=server.serve_forever, daemon=True).start()
        engine.start()
        if not config.listen_early:
            server.server_activate()
        address = f"http://{args.host}:{server.server_port}"
        print_status(engine.ready_line(address))

        def idle_loop() -> None:
            while not engine.closing:
                engine.idle_tick()
                time.sleep(0.1)

        threading.Thread(target=idle_loop, daemon=True).start()
        if config.listen_early:
            while True:
                time.sleep(3600)
        server.serve_forever()
    except StartupFailed as error:
        print_status(f"Error · {error}", error=True)
        exit_code = 1
    except OSError as error:
        print_status(f"Error · unable to start HTTP server: {error}", error=True)
        exit_code = 1
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        def kill(_signum: int, _frame: object) -> None:
            engine.kill_requested.set()

        signal.signal(signal.SIGINT, kill)
        try:
            if engine.backend_created:
                print_status("Stopping · releasing engine resources")  # server/server.py:2049
                engine.closing = True
                if engine.ready and engine.persistent is not None:
                    # runtime/main.mm closePersistentCache: up to 6 s of flush.
                    flushed = not engine.kill_requested.wait(max(0.0, config.flush_seconds))
                    engine.close_persistent(flushed)
                else:
                    engine.kill_requested.wait(0.05)
        finally:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            if server is not None:
                server.server_close()
    return exit_code

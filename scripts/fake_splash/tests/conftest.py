from __future__ import annotations

import http.client
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import FakeSplash

REPO = Path(__file__).resolve().parents[3]
REAL_SPLASH = REPO / "splash"


@pytest.fixture(scope="module")
def engine(tmp_path_factory) -> Iterator[FakeSplash]:
    """One shared fake engine for read-mostly endpoint tests."""
    fake = FakeSplash(tmp_path_factory.mktemp("shared"), env={"FAKE_SPLASH_KEEPALIVE_SECONDS": "0.2"})
    with fake:
        yield fake


@pytest.fixture
def make_engine(tmp_path) -> Iterator:
    """Factory for per-test engines; all are stopped afterwards."""
    started: list[FakeSplash] = []

    def make(**kwargs) -> FakeSplash:
        wait = kwargs.pop("wait", True)
        fake = FakeSplash(kwargs.pop("base", tmp_path), **kwargs)
        started.append(fake)
        return fake.start(wait=wait)

    yield make
    for fake in started:
        if fake.process is not None and fake.process.poll() is None:
            fake.process.kill()
            fake.process.wait()


def sse(engine: FakeSplash, path: str, body: dict, headers: dict | None = None) -> list[tuple[str | None, Any]]:
    """POST and parse an SSE response into (event, data) pairs; data is JSON,
    "[DONE]", or a comment marker (":comment", text)."""
    connection = http.client.HTTPConnection("127.0.0.1", engine.port, timeout=30)
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    if engine.api_key:
        request_headers["Authorization"] = f"Bearer {engine.api_key}"
    connection.request("POST", path, json.dumps(body), request_headers)
    response = connection.getresponse()
    assert response.status == 200, response.read()
    assert response.getheader("Content-Type") == "text/event-stream"
    events: list[tuple[str | None, Any]] = []
    for frame in response.read().decode().split("\n\n"):
        if not frame.strip():
            continue
        event = None
        for line in frame.split("\n"):
            if line.startswith(":"):
                events.append((":comment", line[1:].strip()))
            elif line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
                events.append((event, data if data == "[DONE]" else json.loads(data)))
    connection.close()
    return events


def chat_body(text: str = "hello there", **extra) -> dict:
    return {"messages": [{"role": "user", "content": text}], **extra}

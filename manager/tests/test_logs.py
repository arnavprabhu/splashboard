"""Log stream, crash traces and the diagnostic bundle (SPEC §10.8, docs/api.md §12.2)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui import sse
from splash_gui.logs.api import follow
from splash_gui.paths import Paths
from splash_gui.secrets import SecretName

TRACE = "splash-crash-g1-20261003T100000.json"


@pytest.fixture
def trace_dir(app: FastAPI, tmp_path: Path) -> Path:
    directory = tmp_path / "crash"
    directory.mkdir()
    app.state.manager.crash_trace_dir = directory
    return directory


def test_traces_list_only_splash_traces(client: TestClient, trace_dir: Path) -> None:
    (trace_dir / TRACE).write_text("{}")
    (trace_dir / "notes.txt").write_text("not a trace")
    (trace_dir / ".splash-crash-g1-x.json.tmp").write_text("partial")
    body = client.get("/api/admin/traces").json()
    assert body["directory"] == str(trace_dir) and body["enabled"] is False
    assert [t["name"] for t in body["traces"]] == [TRACE]
    assert body["traces"][0]["size_bytes"] == 2


def test_trace_delete(client: TestClient, trace_dir: Path) -> None:
    (trace_dir / TRACE).write_text("{}")
    (trace_dir / "notes.txt").write_text("keep")
    assert client.delete(f"/api/admin/traces/{TRACE}").status_code == 204
    assert not (trace_dir / TRACE).exists()
    assert client.delete(f"/api/admin/traces/{TRACE}").json()["error"]["code"] == (
        "trace_not_found"
    )
    refused = client.delete("/api/admin/traces/notes.txt")
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "invalid_trace"
    assert (trace_dir / "notes.txt").exists()


def test_diagnostics_redacts_secrets(client: TestClient, app: FastAPI, paths: Paths) -> None:
    key = client.post("/api/admin/settings/secrets/api-key").json()["key"]
    doc = client.get("/api/admin/settings").json()["settings"]
    doc["global"]["chat"]["mcp_servers"] = {
        "web": {"url": "http://localhost:3000/mcp", "headers": {"Authorization": "Bearer abc123"}}
    }
    assert client.put("/api/admin/settings", json=doc).status_code == 200
    paths.engine_log.write_text(f"2026-10-03 10:00:00,000 stdout key={key}\n")
    app.state.manager.raw_status = lambda: {"schema_version": 6, "ready": True}
    body = client.get("/api/admin/diagnostics").json()
    text = json.dumps(body)
    assert key not in text and "abc123" not in text
    assert body["engine_log_tail"] == ["2026-10-03 10:00:00,000 stdout key=••••••"]
    assert body["status"] == {"schema_version": 6, "ready": True}
    assert body["versions"]["engine"]["version"] == "1.2.0"
    assert body["engine"]["state"] == "stopped"
    assert app.state.manager.secrets.get(SecretName.API_KEY) == key


def test_follow_backfills_then_streams_new_lines(tmp_path: Path) -> None:
    log = tmp_path / "engine.log"
    log.write_text("2026-10-03 10:00:00,000 stdout Ready\n")

    async def scenario() -> list[tuple[str, Any]]:
        events = follow(log, "engine", poll=0.01)
        first = await anext(events)
        with log.open("a") as handle:
            handle.write("2026-10-03 10:00:01,000 stderr error: boom\n2026-10-03 10:00:02,0")
        second = await anext(events)
        with log.open("a") as handle:
            handle.write("00 stdout tail\n")
        third = await anext(events)
        # Clear logs truncates the file: the stream starts over from the top.
        log.write_text("2026-10-03 10:00:03,000 stdout after clear\n")
        fourth = await anext(events)
        await events.aclose()
        return [first, second, third, fourth]

    first, second, third, fourth = asyncio.run(scenario())
    assert first == (
        "backfill",
        {
            "lines": [
                {
                    "seq": 0,
                    "ts": first[1]["lines"][0]["ts"],
                    "level": "info",
                    "stream": "stdout",
                    "text": "Ready",
                }
            ]
        },
    )
    assert second[0] == "line" and second[1]["level"] == "error" and second[1]["seq"] == 1
    assert third[1]["text"] == "tail" and third[1]["seq"] == 2
    assert fourth[1]["text"] == "after clear"


def test_follow_survives_rotation(tmp_path: Path) -> None:
    log = tmp_path / "manager.log"
    log.write_text("2026-10-03 10:00:00,000 INFO x: old\n")

    async def scenario() -> tuple[str, Any]:
        events = follow(log, "manager", poll=0.01)
        await anext(events)
        log.replace(tmp_path / "manager.log.1")
        log.write_text("2026-10-03 10:00:01,000 WARNING x: rotated\n")
        item = await anext(events)
        await events.aclose()
        return item

    name, line = asyncio.run(scenario())
    assert name == "line" and line["level"] == "warn" and line["text"] == "x: rotated"


def test_sse_framing_and_ping() -> None:
    async def source() -> AsyncIterator[tuple[str, Any]]:
        yield "hello", {"a": 1}
        await asyncio.sleep(0.05)
        yield "line", {"text": "é"}

    async def collect() -> list[str]:
        return [frame async for frame in sse.frames(source(), ping_interval=0.01)]

    frames = asyncio.run(collect())
    assert frames[0] == "retry: 3000\n\n"
    assert frames[1] == 'id: 1\nevent: hello\ndata: {"a":1}\n\n'
    assert ": ping\n\n" in frames
    assert frames[-1] == 'id: 2\nevent: line\ndata: {"text":"é"}\n\n'

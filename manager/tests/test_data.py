"""Data & privacy sizes and clears (SPEC §10.9, D27)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from .fakeengine import MODEL, EngineHarness


def sizes(client: TestClient) -> dict[str, dict[str, Any]]:
    body = client.get("/api/admin/data/sizes").json()
    return {t["target"]: t for t in body["targets"]}


def test_every_target_reports_a_size(client: TestClient) -> None:
    targets = sizes(client)
    assert set(targets) == {"chats", "usage", "logs", "traces", "kv_cache", "responses", "models"}
    assert targets["responses"]["bytes"] is None and targets["responses"]["note"]
    assert targets["chats"]["items"] == 0 and targets["usage"]["items"] == 0


def test_clear_chats_and_usage(app: FastAPI, client: TestClient) -> None:
    for title in ("one", "two"):
        assert client.post("/api/admin/chats", json={"title": title}).status_code in (200, 201)
    app.state.manager.usage.insert_request({"endpoint": "/v1/chat/completions", "status": 200})
    before = sizes(client)
    assert before["chats"]["items"] == 2 and before["usage"]["items"] == 1
    result = client.post("/api/admin/data/clear", json={"target": "chats"}).json()
    assert result["target"] == "chats" and result["freed_bytes"] > 0
    assert sizes(client)["chats"]["items"] == 0
    assert client.get("/api/admin/chats").json()["chats"] == []
    client.post("/api/admin/data/clear", json={"target": "usage"})
    assert sizes(client)["usage"]["items"] == 0


def test_clear_logs(app: FastAPI, client: TestClient) -> None:
    logs = app.state.manager.paths.logs_dir
    (logs / "engine.log").write_text("x" * 5000)
    (logs / "engine.log.1").write_text("y" * 5000)
    result = client.post("/api/admin/data/clear", json={"target": "logs"}).json()
    assert result["freed_bytes"] >= 10000
    assert not (logs / "engine.log.1").exists()


def test_clear_traces_only_deletes_splash_traces(app: FastAPI, client: TestClient) -> None:
    directory = app.state.manager.crash_trace_dir
    directory.mkdir(parents=True, exist_ok=True)
    trace = directory / "splash-crash-g1-123.json"
    trace.write_text("{}" * 100)
    other = directory / "notes.txt"
    other.write_text("keep")
    assert sizes(client)["traces"]["items"] == 1
    client.post("/api/admin/data/clear", json={"target": "traces"})
    assert not trace.exists() and other.exists()


def test_delete_all_models_is_done_in_the_models_manager(client: TestClient) -> None:
    response = client.post("/api/admin/data/clear", json={"target": "models"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "use_models_manager"


def test_clear_kv_cache_stops_the_engine_and_keeps_tmp(
    harness_factory: Callable[..., EngineHarness],
) -> None:
    h = harness_factory()
    cache = h.state.settings.cache_dir()
    h.load()
    (cache / "namespace-1").mkdir(parents=True, exist_ok=True)
    (cache / "namespace-1" / "blocks").write_bytes(b"k" * 4096)
    (cache / "stray-file").write_bytes(b"s" * 10)
    (cache / "tmp").mkdir(exist_ok=True)
    (cache / "tmp" / "slot").write_bytes(b"t")
    assert sizes(h.client)["kv_cache"]["bytes"] >= 4106
    result = h.client.post("/api/admin/data/clear", json={"target": "kv_cache"}).json()
    assert result["engine_stopped"] is True and result["freed_bytes"] >= 4106
    assert h.engine()["state"] == "stopped"
    assert not (cache / "namespace-1").exists() and not (cache / "stray-file").exists()
    assert (cache / "tmp" / "slot").exists(), "the session-only SSD tier directory stays"


def test_clear_responses_restarts_the_engine(harness_factory: Callable[..., EngineHarness]) -> None:
    h = harness_factory()
    h.load()
    created = h.client.post(
        "/v1/responses", json={"model": MODEL, "input": "hi", "store": True, "max_output_tokens": 4}
    ).json()
    pid = h.engine()["pid"]
    result = h.client.post("/api/admin/data/clear", json={"target": "responses"}).json()
    assert result["engine_restarted"] is True
    view = h.wait_state("ready")
    assert view["pid"] != pid
    assert h.client.get(f"/v1/responses/{created['id']}").status_code == 404


def test_clear_responses_while_stopped_is_a_no_op(client: TestClient) -> None:
    result = client.post("/api/admin/data/clear", json={"target": "responses"}).json()
    assert result["engine_restarted"] is False

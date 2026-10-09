"""Data & privacy sizes and clears."""

from __future__ import annotations

import fcntl
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.data import api as data_api

from .fakeengine import MODEL, EngineHarness


def sizes(client: TestClient) -> dict[str, dict[str, Any]]:
    body = client.get("/api/admin/data/sizes").json()
    return {t["target"]: t for t in body["targets"]}


def test_every_target_reports_a_size(client: TestClient) -> None:
    targets = sizes(client)
    assert set(targets) == {"chats", "usage", "logs", "traces", "kv_cache", "responses", "models"}
    # No engine is running, so no response is stored: a size of 0, not a dash.
    assert targets["responses"]["bytes"] == 0 and targets["responses"]["items"] == 0
    assert targets["responses"]["note"]
    assert targets["chats"]["items"] == 0 and targets["usage"]["items"] == 0


def test_stored_responses_are_sized_from_the_engine(
    harness_factory: Callable[..., EngineHarness],
) -> None:
    """The row reads the engine's `response_store` from its last `/status`."""
    h = harness_factory()
    h.load()
    h.client.post(
        "/v1/responses", json={"model": MODEL, "input": "hi", "store": True, "max_output_tokens": 4}
    )

    def stored() -> dict[str, Any]:
        row: dict[str, Any] = sizes(h.client)["responses"]
        return row

    deadline = time.monotonic() + 10
    while stored()["items"] != 1:  # the status poll runs every 0.2 s under the harness
        assert time.monotonic() < deadline, stored()
        time.sleep(0.05)
    assert stored()["bytes"] > 0
    result = h.client.post("/api/admin/data/clear", json={"target": "responses"}).json()
    assert result["freed_bytes"] > 0 and result["engine_restarted"] is True
    h.wait_state("ready")
    deadline = time.monotonic() + 10
    while stored()["items"] != 0:
        assert time.monotonic() < deadline, stored()
        time.sleep(0.05)
    assert stored()["bytes"] == 0


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
    # Never the developer's real ~/Library/Logs/Splash/crash (it once was).
    assert Path.home() / "Library" not in directory.parents
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
    namespace = cache / ("0123456789abcdef" * 2)  # Splash names namespaces in hex
    namespace.mkdir(parents=True, exist_ok=True)
    (namespace / "kv.slots").write_bytes(b"k" * 4096)
    (namespace / "lock").write_bytes(b"")
    (cache / "tmp").mkdir(exist_ok=True)
    (cache / "tmp" / "slot").write_bytes(b"t")
    assert sizes(h.client)["kv_cache"]["bytes"] >= 4096
    result = h.client.post("/api/admin/data/clear", json={"target": "kv_cache"}).json()
    assert result["engine_stopped"] is True and result["freed_bytes"] >= 4096
    assert h.engine()["state"] == "stopped"
    assert not namespace.exists()
    assert (cache / "tmp" / "slot").exists(), "the session-only SSD tier directory stays"


def test_clear_kv_cache_clears_what_the_engine_actually_wrote(
    harness_factory: Callable[..., EngineHarness],
) -> None:
    # End to end with the fake engine's persistent cache, whose namespace is named
    # as Splash names it (32 hex digits); it used to be `fake-…`, which Clear skips.
    h = harness_factory()
    h.patch_settings({"global": {"serve": {"max_cache_disk": "4G", "persistent_cache": True}}})
    h.load()
    cache = h.state.settings.cache_dir()
    written = [p for p in cache.iterdir() if p.is_dir() and p.name != "tmp"]
    assert written, "the engine opened a persistent cache namespace"
    h.client.post("/api/admin/data/clear", json={"target": "kv_cache"})
    assert h.engine()["state"] == "stopped"
    assert not any(p.exists() for p in written)


def test_clear_kv_cache_only_touches_splash_namespaces(tmp_path: Path) -> None:
    """`storage.cache_dir` may be any directory the user picks; clearing must
    delete only Splash's namespaces (CacheDirectory.cpp plainName), and not one
    another engine still holds."""
    cache = tmp_path / "Documents"
    cache.mkdir()
    (cache / "thesis.docx").write_bytes(b"precious")
    (cache / "Projects").mkdir()
    (cache / "Projects" / "main.py").write_text("print(1)")
    (cache / "cafe").symlink_to(cache / "Projects")  # hex name, but a symlink
    free = cache / ("a" * 32)
    free.mkdir()
    (free / "kv.records").write_bytes(b"r" * 10)
    (free / "lock").write_bytes(b"")
    held = cache / ("b" * 32)
    held.mkdir()
    (held / "lock").write_bytes(b"")
    assert data_api._cache_bytes(cache) == 10
    with (held / "lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # a `splash serve` in a terminal
        assert data_api.clear_kv_cache(cache) == [held.name]
    assert not free.exists() and held.is_dir()
    assert (cache / "thesis.docx").read_bytes() == b"precious"
    assert (cache / "Projects" / "main.py").exists() and (cache / "cafe").is_symlink()


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

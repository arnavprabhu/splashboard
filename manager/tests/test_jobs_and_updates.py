"""Background jobs (`jobs.py`) and engine updates (`engine/updates.py`)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.engine.updates import parse_brew_outdated, version_tuple
from splash_gui.events.bus import EventBus
from splash_gui.jobs import JobFailed, Jobs

from .conftest import write_script
from .fakeengine import MODEL, EngineHarness

# --- jobs.py -------------------------------------------------------------------------------


def record_bus() -> tuple[EventBus, list[tuple[str, Any]]]:
    bus = EventBus()
    seen: list[tuple[str, Any]] = []
    real = bus.publish

    def publish(event: str, data: Any) -> None:
        seen.append((event, data))
        real(event, data)

    bus.publish = publish  # type: ignore[method-assign]
    return bus, seen


def test_a_job_reports_lines_progress_and_done() -> None:
    bus, seen = record_bus()
    jobs = Jobs(bus)

    async def body(job: Any) -> None:
        job.line("first")
        job.update(progress=1.7, message="half")
        assert job.progress == 1.0, "progress is clamped"

    async def main() -> Any:
        accepted = jobs.start("verify", body, model="a/b")
        job = jobs.get(accepted.job_id)
        assert job is not None and job.task is not None
        assert jobs.running("verify", model="a/b") is job
        assert jobs.running("verify", model="c/d") is None
        await job.task
        return job

    job = asyncio.run(main())
    assert job.state == "done" and job.progress == 1.0
    view = job.view()
    assert view.lines == ["first"] and view.model == "a/b"
    events = [d for e, d in seen if e == "job"]
    assert events[0].state == "running" and events[-1].state == "done"
    assert any(e.line == "first" for e in events)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (JobFailed("plain words"), "plain words"),
        (RuntimeError(), "RuntimeError"),
        (ValueError("bad"), "bad"),
    ],
)
def test_a_failing_job_ends_failed_with_a_message(error: Exception, message: str) -> None:
    jobs = Jobs(EventBus())

    async def body(job: Any) -> None:
        raise error

    async def main() -> Any:
        accepted = jobs.start("import", body)
        job = jobs.get(accepted.job_id)
        assert job is not None and job.task is not None
        await job.task
        return job

    job = asyncio.run(main())
    assert job.state == "failed" and job.message == message
    assert jobs.running("import") is None


def test_shutdown_cancels_running_jobs() -> None:
    jobs = Jobs(EventBus())

    async def body(job: Any) -> None:
        await asyncio.sleep(60)

    async def main() -> Any:
        accepted = jobs.start("storage_move", body)
        await asyncio.sleep(0)
        await jobs.shutdown()
        return jobs.get(accepted.job_id)

    job = asyncio.run(main())
    assert job.state == "failed" and job.message == "cancelled"
    assert [j.id for j in jobs.all()] == [job.id]


# --- updates.py ----------------------------------------------------------------------------


def test_version_parsing() -> None:
    assert version_tuple("Splash 1.2.0") == (1, 2, 0)
    assert version_tuple("v1.10.3-rc1") == (1, 10, 3)
    assert version_tuple(None) is None and version_tuple("dev") is None
    outdated = {"formulae": [{"name": "incoai/tap/splash", "current_version": "1.4.0"}]}
    assert parse_brew_outdated(json.dumps(outdated)) == "1.4.0"
    assert parse_brew_outdated('{"formulae": []}') is None
    assert parse_brew_outdated("not json") is None


def releases(tag: str, body: str = "Notes") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        return httpx.Response(
            200, json={"tag_name": tag, "body": body, "html_url": f"https://github.com/r/{tag}"}
        )

    return httpx.MockTransport(handler)


def test_check_finds_a_newer_release(app: FastAPI, client: TestClient) -> None:
    updates = app.state.manager.updates
    updates.find_brew = lambda: None
    updates.transport = releases("v1.4.0", "## What's new")
    info = client.post("/api/admin/engine/check-update").json()
    assert info["available"] is True and info["version"] == "1.4.0"
    assert info["release_notes_md"] == "## What's new" and info["checked_at"]
    assert client.get("/api/admin/versions").json()["engine_update"]["version"] == "1.4.0"
    alerts = client.get("/api/admin/alerts").json()["alerts"]
    alert = next(a for a in alerts if a["condition"] == "update_available")
    assert alert["severity"] == "info" and "1.4.0" in alert["title"]


def test_check_same_or_older_is_not_an_update(app: FastAPI, client: TestClient) -> None:
    updates = app.state.manager.updates
    updates.find_brew = lambda: None
    updates.transport = releases("v1.3.0")
    info = client.post("/api/admin/engine/check-update").json()
    assert info["available"] is False and info["version"] is None


def test_check_prefers_brew_outdated(app: FastAPI, client: TestClient, tmp_path: Path) -> None:
    outdated = {"formulae": [{"name": "splash", "current_version": "1.3.5"}]}
    brew = write_script(tmp_path / "brew", f"echo '{json.dumps(outdated)}'\n")
    updates = app.state.manager.updates
    updates.find_brew = lambda: str(brew)
    updates.transport = releases("v1.4.0")
    info = client.post("/api/admin/engine/check-update").json()
    assert info["version"] == "1.3.5", "brew knows what the tap actually ships"


def test_check_offline_is_harmless(app: FastAPI, client: TestClient) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    updates = app.state.manager.updates
    updates.find_brew = lambda: None
    updates.transport = httpx.MockTransport(down)
    info = client.post("/api/admin/engine/check-update").json()
    assert info["available"] is False and info["checked_at"]


def wait_job(client: TestClient, job_id: str, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        view = client.get(f"/api/admin/jobs/{job_id}").json()
        if view["state"] != "running":
            return dict(view)
        assert time.monotonic() < deadline, view
        time.sleep(0.05)


def test_upgrade_stops_upgrades_and_restarts_the_model(
    harness_factory: Callable[..., EngineHarness], tmp_path: Path
) -> None:
    h = harness_factory()
    h.load()
    log = tmp_path / "brew.log"
    brew = write_script(tmp_path / "brew", f'echo "$@" >> "{log}"\necho "==> $1 done"\n')
    h.state.updates.find_brew = lambda: str(brew)
    first_pid = h.engine()["pid"]
    accepted = h.client.post("/api/admin/engine/upgrade")
    assert accepted.status_code == 202
    job = wait_job(h.client, accepted.json()["job_id"])
    assert job["state"] == "done", job
    assert log.read_text().splitlines() == ["update", "upgrade incoai/tap/splash"]
    assert "==> update done" in job["lines"]
    view = h.wait_state("ready")
    assert view["model"] == MODEL and view["pid"] != first_pid
    assert [s["reason"] for s in h.state.usage.sessions()][1] == "engine_upgrade"


def test_upgrade_failure_is_reported(
    harness_factory: Callable[..., EngineHarness], tmp_path: Path
) -> None:
    h = harness_factory(installed=None)
    brew = write_script(tmp_path / "brew", 'if [ "$1" = upgrade ]; then echo boom; exit 3; fi\n')
    h.state.updates.find_brew = lambda: str(brew)
    job = wait_job(h.client, h.client.post("/api/admin/engine/upgrade").json()["job_id"])
    assert job["state"] == "failed" and "exited 3" in job["message"]


def test_a_failed_upgrade_brings_the_model_back(
    harness_factory: Callable[..., EngineHarness], tmp_path: Path
) -> None:
    h = harness_factory()
    h.load()
    brew = write_script(tmp_path / "brew", 'if [ "$1" = upgrade ]; then echo boom; exit 3; fi\n')
    h.state.updates.find_brew = lambda: str(brew)
    job = wait_job(h.client, h.client.post("/api/admin/engine/upgrade").json()["job_id"])
    assert job["state"] == "failed"
    view = h.wait_state("ready")
    assert view["model"] == MODEL


def test_upgrade_without_homebrew(app: FastAPI, client: TestClient) -> None:
    app.state.manager.updates.find_brew = lambda: None
    job = wait_job(client, client.post("/api/admin/engine/upgrade").json()["job_id"])
    assert job["state"] == "failed" and "Homebrew" in job["message"]


def test_install_runs_brew_install(app: FastAPI, client: TestClient, tmp_path: Path) -> None:
    log = tmp_path / "brew.log"
    brew = write_script(tmp_path / "brew", f'echo "$@" >> "{log}"\n')
    app.state.manager.updates.find_brew = lambda: str(brew)
    job = wait_job(client, client.post("/api/admin/engine/install").json()["job_id"])
    assert job["state"] == "done", job
    assert log.read_text().strip() == "install incoai/tap/splash"
    assert job["message"].startswith("Splash 1.3.0")

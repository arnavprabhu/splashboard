"""`settings.changed` after every successful settings write, from any client (docs/api.md §4)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _announced(app: FastAPI) -> list[dict[str, Any]]:
    """Every `settings.changed` payload published from now on, in order."""
    seen: list[dict[str, Any]] = []
    app.state.manager.events.listeners.append(
        lambda event, data: seen.append(data) if event == "settings.changed" else None
    )
    return seen


def _document(client: TestClient) -> dict[str, Any]:
    return dict(client.get("/api/admin/settings").json()["settings"])


def test_a_saved_change_is_announced_with_its_key(app: FastAPI, client: TestClient) -> None:
    seen = _announced(app)
    doc = _document(client)
    doc["global"]["ui"]["theme"] = "dark" if doc["global"]["ui"]["theme"] == "light" else "light"
    response = client.put("/api/admin/settings", json=doc)
    assert response.status_code == 200, response.text
    assert len(seen) == 1, seen
    assert [change["key"] for change in seen[0]["changed"]] == ["ui.theme"]
    assert seen[0]["changed"] == response.json()["changed"]
    assert seen[0]["restart_required"] is False


def test_a_rejected_save_is_not_announced(app: FastAPI, client: TestClient) -> None:
    seen = _announced(app)
    doc = _document(client)
    doc["global"]["ui"]["theme"] = "neon"
    response = client.put("/api/admin/settings", json=doc)
    assert response.status_code == 422, response.text
    assert seen == []


def test_an_unchanged_save_is_still_announced_with_no_changes(
    app: FastAPI, client: TestClient
) -> None:
    seen = _announced(app)
    response = client.put("/api/admin/settings", json=_document(client))
    assert response.status_code == 200, response.text
    assert seen == [{"changed": [], "restart_required": False}]


def test_a_reset_is_announced_too(app: FastAPI, client: TestClient) -> None:
    seen = _announced(app)
    response = client.post("/api/admin/settings/reset", json={})
    assert response.status_code == 200, response.text
    assert len(seen) == 1, seen

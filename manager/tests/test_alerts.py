"""Health alerts and notifications (SPEC §16.3, `events/alerts.py`)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.events.alerts import NOTIFY_INTERVAL_S, AlertCenter


def center(app: FastAPI) -> tuple[AlertCenter, list[tuple[str, Any]], list[tuple[str, str]]]:
    alerts: AlertCenter = app.state.manager.alerts
    published: list[tuple[str, Any]] = []
    shown: list[tuple[str, str]] = []
    real = alerts.bus.publish

    def spy(event: str, data: Any) -> None:
        published.append((event, data))
        real(event, data)

    alerts.bus.publish = spy  # type: ignore[method-assign]
    alerts.osascript = lambda title, body: shown.append((title, body))
    return alerts, published, shown


def test_a_recurring_condition_bumps_the_count(app: FastAPI) -> None:
    alerts, published, _ = center(app)
    first = alerts.raise_alert("queue_full", "Queue full (32)", source="proxy")
    second = alerts.raise_alert("queue_full", "Queue full (32)", source="proxy")
    assert first.id == second.id == "queue_full" and second.count == 2
    assert second.severity == "warn"
    assert [e for e, _ in published].count("alert") == 2


def test_notifications_are_rate_limited_per_condition(app: FastAPI) -> None:
    alerts, published, shown = center(app)
    now = [1000.0]
    alerts.clock = lambda: now[0]
    for _ in range(3):
        alerts.raise_alert("engine_failed", "Engine stopped", source="status")
    notes = [d for e, d in published if e == "notification"]
    assert len(notes) == 1, "one notification per condition every 10 minutes"
    assert shown == [("Engine stopped", "")], "osascript fallback without a menu bar app"
    now[0] += NOTIFY_INTERVAL_S + 1
    alerts.raise_alert("engine_failed", "Engine stopped", source="status")
    assert len([d for e, d in published if e == "notification"]) == 2


def test_download_notifications_are_per_item(app: FastAPI) -> None:
    """D88: a download alert's rate key is its item id, so two downloads that finish
    inside 10 minutes both notify, and a repeat of one item does not."""
    alerts, published, _ = center(app)
    alerts.clock = lambda: 1000.0
    alerts.raise_alert("download_done", "A downloaded", source="downloader", subject="item-a")
    alerts.raise_alert("download_done", "B downloaded", source="downloader", subject="item-b")
    alerts.raise_alert("download_done", "A downloaded", source="downloader", subject="item-a")
    titles = [d.title for e, d in published if e == "notification"]
    assert titles == ["A downloaded", "B downloaded"]


def test_banner_only_conditions_never_notify(app: FastAPI) -> None:
    alerts, published, _ = center(app)
    alerts.raise_alert("memory_warning", "Memory pressure is high", source="status")
    alerts.raise_alert("schema_unrecognized", "schema 7", source="status")
    assert not [d for e, d in published if e == "notification"]
    assert {a.condition for a in alerts.all()} == {"memory_warning", "schema_unrecognized"}


def test_notification_settings_silence_their_kinds(app: FastAPI) -> None:
    alerts, published, _ = center(app)
    store = app.state.manager.settings
    document = store.current.model_dump(mode="json", by_alias=True)
    document["global"]["notifications"]["engine_failed"] = False
    assert store.save(document)[0].ok
    alerts.raise_alert("crash_loop", "Splash crashed 3× in 5 min", source="supervisor")
    assert alerts.active("crash_loop"), "the banner still shows"
    assert not [d for e, d in published if e == "notification"]


def test_alerts_are_ordered_and_dismissed(app: FastAPI, client: TestClient) -> None:
    alerts, _, _ = center(app)
    alerts.raise_alert("write_behind_refused", "cap", source="status")
    alerts.raise_alert("memory_critical", "critical", source="status")
    alerts.raise_alert("queue_full", "queue", source="proxy")
    listed = client.get("/api/admin/alerts").json()["alerts"]
    assert [a["severity"] for a in listed] == ["critical", "warn", "info"]
    assert client.post("/api/admin/alerts/queue_full/dismiss").status_code == 200
    assert client.post("/api/admin/alerts/queue_full/dismiss").status_code == 404
    alerts.raise_alert(
        "unclean_integration_shutdown", "x", source="integrations", dismissible=False
    )
    refused = client.post("/api/admin/alerts/unclean_integration_shutdown/dismiss")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "not_dismissible"
    alerts.clear_condition("memory_critical")
    assert not alerts.active("memory_critical")

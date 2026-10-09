"""`POST /app/check-updates` (SPEC §19, docs/ui/05 §3.17; PKG-9): the web About page asks the
menu bar app to run its Sparkle check. The manager only relays it as an `app.check_updates`
event to the streams the app opened with `client=menubar`; with none open it answers 409 and
the web says `Open the menu bar app to update.`"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from splash_gui.events.alerts import MENUBAR_CLIENT
from splash_gui.events.bus import Subscription


def _published(app: FastAPI) -> list[tuple[str, Any]]:
    seen: list[tuple[str, Any]] = []
    app.state.manager.events.listeners.append(lambda event, data: seen.append((event, data)))
    return seen


def _subscribe(app: FastAPI, client: str | None) -> Subscription:
    bus = app.state.manager.events
    sub = Subscription(asyncio.Queue(), client)
    with bus._lock:
        bus._subs.add(sub)
    return sub


def test_without_the_menu_bar_app_the_check_is_refused(app: FastAPI, client: TestClient) -> None:
    seen = _published(app)
    _subscribe(app, None)  # a web page's stream is not the app

    reply = client.post("/api/admin/app/check-updates")

    assert reply.status_code == 409
    body = reply.json()
    assert body["error"]["code"] == "app_not_running"
    assert body["error"]["message"] == "Open the menu bar app to update."
    assert not [event for event, _ in seen if event == "app.check_updates"]


def test_with_the_menu_bar_app_connected_the_check_is_relayed(
    app: FastAPI, client: TestClient
) -> None:
    seen = _published(app)
    _subscribe(app, MENUBAR_CLIENT)

    reply = client.post("/api/admin/app/check-updates")

    assert reply.status_code == 202
    assert reply.json() == {"ok": True}
    assert [event for event, _ in seen if event == "app.check_updates"] == ["app.check_updates"]


def test_the_check_needs_a_credential(app: FastAPI, browser: TestClient) -> None:
    _subscribe(app, MENUBAR_CLIENT)

    assert browser.post("/api/admin/app/check-updates").status_code == 401

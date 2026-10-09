"""Health alerts and user notifications (SPEC §16.3, docs/api.md §4.1).

An alert is a standing condition shown in the global band until it clears or is
dismissed; recurrences bump `count`. A notification is a one-off message for the
menu bar app (or `osascript` when no menu bar app is listening, SPEC §4.2),
rate-limited to one per alert id every 10 minutes (the id is the condition, or
`condition:subject` for a download, so each download notifies once; D88) and
filtered by the `notifications.*` settings.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from ..schemas import Alert, AlertCleared, AlertCondition, Notification, NotificationAction
from ..settings.store import SettingsStore
from .bus import EventBus

log = logging.getLogger(__name__)

Severity = Literal["info", "warn", "critical"]
Source = Literal["status", "proxy", "supervisor", "downloader", "updater", "integrations"]

NOTIFY_INTERVAL_S = 600.0
OSASCRIPT_ENV = "SPLASH_GUI_OSASCRIPT"  # "0" disables the fallback (tests)
MENUBAR_CLIENT = "menubar"

SEVERITY: dict[str, Severity] = {
    "engine_recovering": "warn",
    "engine_failed": "critical",
    "crash_loop": "critical",
    "memory_critical": "critical",
    "memory_warning": "warn",
    "capacity_exhausted": "warn",
    "resource_timeout": "warn",
    "disk_tier_failures": "warn",
    "write_behind_refused": "info",
    "queue_full": "warn",
    "mask_timeout": "info",
    "download_failed": "warn",
    "download_done": "info",
    "update_available": "info",
    "schema_unrecognized": "warn",
    "unclean_integration_shutdown": "warn",
}
# notifications.<key> that silences a kind; kinds not listed always notify.
NOTIFICATION_SETTING: dict[str, str] = {
    "download_done": "download_done",
    "download_failed": "download_done",
    "engine_failed": "engine_failed",
    "crash_loop": "engine_failed",
    "memory_critical": "memory_critical",
    "update_available": "update_available",
    "disk_tier_failures": "disk_cache_errors",
    "write_behind_refused": "disk_cache_errors",
}
# Conditions that are banner-only (SPEC §16.3 "warn (banner only)").
BANNER_ONLY = frozenset({"memory_warning", "schema_unrecognized", "mask_timeout"})
_RANK = {"critical": 0, "warn": 1, "info": 2}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def action(
    id: str, label: str, path: str, method: str = "POST", body: dict[str, Any] | None = None
) -> NotificationAction:
    return NotificationAction(id=id, label=label, method=method, path=path, body=body)  # type: ignore[arg-type]


RESTART_ACTION = action("restart", "Restart", "/api/admin/engine/restart")
LOGS_ACTION = action("logs", "Logs", "/admin/logs", method="GET")


@dataclass
class AlertCenter:
    bus: EventBus
    settings: SettingsStore
    _alerts: dict[str, Alert] = field(default_factory=dict)
    _dismissed: set[str] = field(default_factory=set)
    _notified: dict[str, float] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    clock: Callable[[], float] = time.monotonic
    osascript: Callable[[str, str], None] | None = None

    # Alerts --------------------------------------------------------------------

    def raise_alert(
        self,
        condition: AlertCondition,
        title: str,
        message: str = "",
        *,
        source: Source,
        subject: str | None = None,
        severity: Severity | None = None,
        actions: list[NotificationAction] | None = None,
        dismissible: bool = True,
        notify: bool = True,
    ) -> Alert:
        alert_id = condition if subject is None else f"{condition}:{subject}"
        now = now_iso()
        with self._lock:
            existing = self._alerts.get(alert_id)
            if existing is not None:
                alert = existing.model_copy(
                    update={
                        "title": title,
                        "message": message,
                        "updated_at": now,
                        "count": existing.count + 1,
                        "actions": actions if actions is not None else existing.actions,
                    }
                )
            else:
                alert = Alert(
                    id=alert_id,
                    condition=condition,
                    severity=severity or SEVERITY.get(condition, "warn"),
                    title=title,
                    message=message,
                    source=source,
                    raised_at=now,
                    updated_at=now,
                    count=1,
                    dismissible=dismissible,
                    actions=list(actions or []),
                )
            self._alerts[alert_id] = alert
            self._dismissed.discard(alert_id)
        self.bus.publish("alert", alert)
        if notify and condition not in BANNER_ONLY:
            self.notify(
                condition,
                title,
                message,
                actions=alert.actions,
                key=alert_id,
            )
        return alert

    def clear(self, alert_id: str) -> bool:
        with self._lock:
            removed = self._alerts.pop(alert_id, None)
        if removed is not None:
            self.bus.publish("alert.cleared", AlertCleared(id=alert_id))
        return removed is not None

    def clear_condition(self, condition: str) -> None:
        with self._lock:
            ids = [a.id for a in self._alerts.values() if a.condition == condition]
        for alert_id in ids:
            self.clear(alert_id)

    def dismiss(self, alert_id: str) -> Literal["dismissed", "not_found", "not_dismissible"]:
        with self._lock:
            alert = self._alerts.get(alert_id)
            if alert is None:
                return "not_found"
            if not alert.dismissible:
                return "not_dismissible"
            self._dismissed.add(alert_id)
        self.clear(alert_id)
        return "dismissed"

    def active(self, condition: str) -> bool:
        with self._lock:
            return any(a.condition == condition for a in self._alerts.values())

    def all(self) -> list[Alert]:
        with self._lock:
            alerts = list(self._alerts.values())
        return sorted(alerts, key=lambda a: (_RANK[a.severity], a.updated_at), reverse=False)

    def get(self, alert_id: str) -> Alert | None:
        with self._lock:
            return self._alerts.get(alert_id)

    # Notifications -------------------------------------------------------------

    def _enabled(self, kind: str) -> bool:
        key = NOTIFICATION_SETTING.get(kind)
        if key is None:
            return True
        settings = self.settings.current.global_.notifications
        return bool(getattr(settings, key, True))

    def notify(
        self,
        kind: AlertCondition | Literal["info"],
        title: str,
        body: str = "",
        *,
        actions: list[NotificationAction] | None = None,
        key: str | None = None,
        force: bool = False,
    ) -> Notification | None:
        """Publish a notification unless it is off in settings or rate-limited."""
        if not self._enabled(kind):
            return None
        rate_key = key or f"{kind}:{title}"
        now = self.clock()
        with self._lock:
            last = self._notified.get(rate_key)
            if not force and last is not None and now - last < NOTIFY_INTERVAL_S:
                return None
            self._notified[rate_key] = now
        notification = Notification(
            id=uuid.uuid4().hex,
            kind=kind,
            title=title,
            body=body,
            ts=now_iso(),
            actions=list(actions or []),
        )
        self.bus.publish("notification", notification)
        if self.bus.subscriber_count(MENUBAR_CLIENT) == 0:
            self._fallback(title, body)
        return notification

    def _fallback(self, title: str, body: str) -> None:
        if self.osascript is not None:
            self.osascript(title, body)
            return
        if os.environ.get(OSASCRIPT_ENV, "1") == "0":
            return
        osascript = shutil.which("osascript")
        if osascript is None:
            return

        def esc(text: str) -> str:
            return text.replace("\\", "\\\\").replace('"', '\\"')

        script = f'display notification "{esc(body)}" with title "{esc(title)}"'
        threading.Thread(
            target=lambda: subprocess.run(
                [osascript, "-e", script], capture_output=True, timeout=10, check=False
            ),
            daemon=True,
        ).start()

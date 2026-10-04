"""Engine updates and installation (SPEC §6.7, §10.2 step 1).

- **Check** once a day (and on demand): `brew outdated --json=v2 incoai/tap/splash`,
  then the GitHub releases API for the version and release notes.
- **Upgrade**: stop the engine, `brew update && brew upgrade incoai/tap/splash` with
  every line on `/events` (`engine.upgrade` and `job`), rediscover, restart the
  model that was active.
- **Install**: `brew install incoai/tap/splash` for the welcome wizard.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..events.alerts import action
from ..jobs import Job, JobFailed
from ..schemas import EngineUpgradeEvent, JobAccepted, UpdateInfo

if TYPE_CHECKING:
    from ..state import ManagerState

log = logging.getLogger(__name__)

FORMULA = "incoai/tap/splash"
RELEASES_URL = "https://api.github.com/repos/incoai/splash/releases/latest"
CHECK_INTERVAL_S = 24 * 3600
BREW_CANDIDATES = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def find_brew() -> str | None:
    return shutil.which("brew") or next((b for b in BREW_CANDIDATES if Path(b).exists()), None)


def version_tuple(text: str | None) -> tuple[int, int, int] | None:
    match = _VERSION.search(text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def parse_brew_outdated(text: str) -> str | None:
    """The newer version `brew outdated --json=v2` reports for splash, if any."""
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return None
    for formula in data.get("formulae", []) if isinstance(data, dict) else []:
        if isinstance(formula, dict) and str(formula.get("name", "")).endswith("splash"):
            current = formula.get("current_version")
            return str(current) if current else None
    return None


class UpdateService:
    def __init__(self, state: ManagerState) -> None:
        self.state = state
        self.engine_update = UpdateInfo()
        self._task: asyncio.Task[None] | None = None
        self._upgrading = False
        # Seams for tests: the GitHub transport and the brew lookup.
        self.transport: httpx.AsyncBaseTransport | None = None
        self.find_brew = find_brew

    async def start(self) -> None:
        self.state.updates = self
        if self.state.update_check:
            self._task = asyncio.create_task(self._loop(), name="engine-update-check")

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task

    async def _loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            with contextlib.suppress(Exception):
                await self.check()
            await asyncio.sleep(CHECK_INTERVAL_S)

    async def check(self) -> UpdateInfo:
        engine = await asyncio.to_thread(self.state.engine)
        installed = version_tuple(engine.version)
        latest: str | None = None
        brew = self.find_brew()
        if brew is not None and engine.source == "brew":
            with contextlib.suppress(Exception):
                proc = await asyncio.create_subprocess_exec(
                    brew,
                    "outdated",
                    "--json=v2",
                    FORMULA,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), 60)
                latest = parse_brew_outdated(out.decode("utf-8", "replace"))
        notes: str | None = None
        url: str | None = None
        release: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            async with httpx.AsyncClient(timeout=10, transport=self.transport) as client:
                response = await client.get(
                    RELEASES_URL, headers={"Accept": "application/vnd.github+json"}
                )
                if response.status_code == 200:
                    release = response.json()
        if release:
            tag = str(release.get("tag_name") or release.get("name") or "")
            notes = release.get("body") if isinstance(release.get("body"), str) else None
            url = release.get("html_url") if isinstance(release.get("html_url"), str) else None
            if latest is None and tag:
                latest = tag.lstrip("v")
        newer = (
            latest is not None
            and installed is not None
            and (version_tuple(latest) or (0, 0, 0)) > installed
        )
        self.engine_update = UpdateInfo(
            available=bool(newer),
            version=latest if newer else None,
            release_notes_md=notes if newer else None,
            url=url,
            checked_at=datetime.now(UTC).isoformat(),
        )
        if newer and latest is not None:
            self.state.alerts.raise_alert(
                "update_available",
                f"Splash {latest} available",
                "Upgrade from Settings → About.",
                source="updater",
                actions=[action("upgrade", "Upgrade", "/api/admin/engine/upgrade")],
            )
        return self.engine_update

    # Jobs ---------------------------------------------------------------------------

    def _event(self, phase: str, line: str | None = None, ok: bool | None = None) -> None:
        self.state.events.publish(
            "engine.upgrade",
            EngineUpgradeEvent(phase=phase, line=line, ok=ok),  # type: ignore[arg-type]
        )

    async def _run(self, job: Job, argv: list[str], phase: str) -> int:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env={**_brew_env()},
        )
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            job.line(line)
            if job.kind == "engine_upgrade":
                self._event(phase, line)
        return await proc.wait()

    def upgrade(self) -> JobAccepted:
        if self.state.jobs.running("engine_upgrade") is not None:
            from ..errors import ApiError

            raise ApiError(409, "An engine upgrade is already running", "job_running")

        async def body(job: Job) -> None:
            brew = self.find_brew()
            if brew is None:
                self._event("failed", "Homebrew is not installed", False)
                raise JobFailed("Homebrew is not installed")
            sup = self.state.supervisor
            previous = sup.active_model() if sup is not None else None
            self._event("stopping")
            if sup is not None and previous is not None:
                await sup.stop(reason="engine_upgrade")
            self._event("updating")
            job.update(progress=0.1, message="brew update")
            if await self._run(job, [brew, "update"], "updating") != 0:
                self._event("failed", "brew update failed", False)
                raise JobFailed("brew update failed")
            self._event("upgrading")
            job.update(progress=0.4, message="brew upgrade")
            code = await self._run(job, [brew, "upgrade", FORMULA], "upgrading")
            if code != 0:
                self._event("failed", f"brew upgrade exited {code}", False)
                raise JobFailed(f"brew upgrade exited {code}")
            self._event("rediscovering")
            job.update(progress=0.8, message="rediscovering")
            self.state.forget_engine()
            self.state.engine_options.clear()
            await asyncio.to_thread(self.state.engine, True)
            if sup is not None and previous is not None:
                self._event("restarting")
                await sup.load(previous, reason="engine_upgrade")
            self._event("done", None, True)
            self.engine_update = UpdateInfo(checked_at=datetime.now(UTC).isoformat())
            self.state.alerts.clear_condition("update_available")

        return self.state.jobs.start("engine_upgrade", body)

    def install(self) -> JobAccepted:
        if self.state.jobs.running("engine_install") is not None:
            from ..errors import ApiError

            raise ApiError(409, "Splash is already being installed", "job_running")

        async def body(job: Job) -> None:
            brew = self.find_brew()
            if brew is None:
                raise JobFailed("Homebrew is not installed; install it from https://brew.sh first")
            job.update(progress=0.05, message=f"brew install {FORMULA}")
            code = await self._run(job, [brew, "install", FORMULA], "upgrading")
            if code != 0:
                raise JobFailed(f"brew install exited {code}")
            self.state.forget_engine()
            info = await asyncio.to_thread(self.state.engine, True)
            if not info.found:
                raise JobFailed(info.error or "Splash was installed but cannot be found")
            job.update(message=f"Splash {info.version} installed")

        return self.state.jobs.start("engine_install", body)


def _brew_env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.setdefault("HOMEBREW_NO_AUTO_UPDATE", "1")
    env["HOMEBREW_NO_ENV_HINTS"] = "1"
    env["NONINTERACTIVE"] = "1"
    return env


def create(state: ManagerState) -> UpdateService:
    service = UpdateService(state)
    state.updates = service
    return service

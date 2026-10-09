"""The compatibility check's runs and per-variant verdicts.

A run is one helper subprocess (`helpers/inspect_model.py`) for `model@sha`, shared
by every caller that asks while it runs (`GET /inspect` waits for the end,
`POST /inspect/stream` follows it). The helper prints the variant table at once,
then one verdict per variant as it finishes, the variant the GUI expects to
recommend first; each verdict is stored here by `repo@sha` and kept a day, so a
later check of the same repository (or of one variant of it) reuses them and only
runs Splash for what is missing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..errors import ApiError

TTL_S = 86400.0


@dataclass
class Verdicts:
    """What Splash said about one repository at one commit."""

    table: list[dict[str, Any]] | None = None
    # name (None for a repository without variants) -> (checked at, helper result)
    results: dict[str | None, tuple[float, dict[str, Any]]] = field(default_factory=dict)

    def fresh(self, now: float | None = None) -> dict[str | None, dict[str, Any]]:
        now = time.time() if now is None else now
        return {name: raw for name, (at, raw) in self.results.items() if now - at < TTL_S}

    def checked_at(self, names: list[str | None]) -> float | None:
        times = [self.results[n][0] for n in names if n in self.results]
        return max(times) if times else None


def expected_names(table: list[dict[str, Any]] | None, variant: str | None) -> list[str | None]:
    """The verdicts a check of `REPO` or `REPO:VARIANT` consists of: the requested
    variant, else every variant in the table, else the repository itself (None)."""
    if variant is not None:
        return [variant]
    if table:
        return [row["name"] for row in table]
    return [None]


@dataclass
class Run:
    """One helper run; listeners are woken on every line and at the end."""

    key: str
    model: str
    repo_id: str
    variant: str | None
    repo: Any
    store: Verdicts
    first: str | None = None
    started: float = field(default_factory=time.time)
    error: ApiError | None = None
    task: asyncio.Task[None] | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    listeners: set[asyncio.Queue[None]] = field(default_factory=set)

    def notify(self) -> None:
        for queue in self.listeners:
            queue.put_nowait(None)

    def subscribe(self) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue()
        self.listeners.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[None]) -> None:
        self.listeners.discard(queue)


async def run_helper(
    run: Run,
    argv: list[str],
    env: dict[str, str],
    spec: dict[str, Any],
    timeout: float,
) -> None:
    """Start the helper, store each line's news in `run.store` as it arrives, and
    set `run.error` when Splash's check cannot finish. Partial verdicts stay stored."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 << 20,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    proc.stdin.write(json.dumps(spec).encode())
    proc.stdin.close()
    stderr = asyncio.ensure_future(proc.stderr.read())

    async def follow() -> None:
        assert proc.stdout is not None
        async for line in proc.stdout:
            if not line.strip():
                continue
            message = json.loads(line)
            if "variants" in message:
                run.store.table = message["variants"]
                run.first = message.get("first")
            elif "result" in message:
                result = message["result"]
                run.store.results[result.get("name")] = (time.time(), result)
            run.notify()
        await proc.wait()

    try:
        async with asyncio.timeout(timeout):
            await follow()
    except TimeoutError:
        run.error = ApiError(503, "Compatibility check timed out", "inspect_timeout")
    except (ValueError, KeyError) as error:
        run.error = ApiError(503, f"Splash compatibility helper failed: {error}", "inspect_failed")
    finally:
        if proc.returncode is None:
            proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
        err = await stderr
    if run.error is None and proc.returncode:
        run.error = ApiError(
            503,
            "Splash compatibility helper failed: " + err.decode(errors="replace")[-1500:],
            "inspect_failed",
        )

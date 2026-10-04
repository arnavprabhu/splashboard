"""The subsystem lifecycle: each area creates a `Service` that the app lifespan
starts in order and shuts down in reverse (SPEC §4.1)."""

from __future__ import annotations

from typing import Protocol


class Service(Protocol):
    async def start(self) -> None: ...

    async def shutdown(self) -> None: ...

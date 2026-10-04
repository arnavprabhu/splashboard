"""Repeatable benchmark runs with cancellation and persistent results (SPEC §10.6)."""

from __future__ import annotations

import asyncio
import statistics
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx

from ..errors import ApiError
from ..proxy.shapes import UsageCapture
from ..schemas import (
    BenchmarkPreflight,
    BenchmarkProgressEvent,
    BenchmarkRequest,
    BenchmarkResult,
    BenchmarkRun,
    BenchmarkStarted,
)
from ..system.info import system_info
from ..usage.db import iso

if TYPE_CHECKING:
    from ..state import ManagerState


class Benchmark:
    def __init__(self, state: ManagerState) -> None:
        self.state = state
        self.task: asyncio.Task[None] | None = None
        self.current: BenchmarkRun | None = None

    async def start(self) -> None:
        # A run interrupted by a manager crash cannot remain "running" forever.
        for run in self.state.usage.benchmarks():
            if run["state"] == "running":
                run["state"], run["error"] = "failed", "Manager stopped during this run"
                self.state.usage.save_benchmark(run)

    def preflight(self) -> BenchmarkPreflight:
        warnings = []
        info = system_info(self.state.settings.models_dir(), self.state.settings.cache_dir())
        if info.power.source == "battery":
            warnings.append("This Mac is on battery power.")
        if info.power.low_power_mode:
            warnings.append("Low Power Mode is enabled.")
        if self.state.supervisor.busy():
            warnings.append("Other requests are in flight; results may be affected.")
        return BenchmarkPreflight(ready=self.state.supervisor.accepting, warnings=warnings)

    def launch(self, body: BenchmarkRequest) -> BenchmarkStarted:
        if self.task and not self.task.done():
            raise ApiError(409, "A benchmark is already running", "benchmark_running")
        if not self.state.supervisor.accepting:
            raise ApiError(503, "Load a model before benchmarking", "engine_unavailable")
        if (
            not body.scenarios
            or any(n not in (2048, 8192, 32768) for n in body.prefill_tokens)
            or any(n not in (1, 2, 3, 4) for n in body.concurrency)
        ):
            raise ApiError(400, "Invalid benchmark parameters", "invalid_benchmark")
        info = system_info(self.state.settings.models_dir(), self.state.settings.cache_dir())
        self.current = BenchmarkRun(
            id=uuid.uuid4().hex,
            ts=iso(),
            state="running",
            model=self.state.active_model() or "",
            engine_version=self.state.engine_cached().version,
            hardware=info.model_dump(exclude={"power"}),
            power=info.power.model_dump(),
            settings=self.state.settings.current.model_dump(mode="json", by_alias=True),
            results=[],
        )
        self.state.usage.save_benchmark(self.current.model_dump())
        self.task = asyncio.create_task(self.run(body, self.current))
        return BenchmarkStarted(run_id=self.current.id)

    async def sample(
        self, client: httpx.AsyncClient, prompt: list[int], tokens: int
    ) -> dict[str, Any]:
        capture = UsageCapture(shape="completions", endpoint="/v1/completions", stream=True)
        started = time.monotonic()
        self.state.supervisor.request_started()
        try:
            async with client.stream(
                "POST",
                "/v1/completions",
                json={
                    "prompt": prompt,
                    "model": self.state.active_model(),
                    "max_tokens": tokens,
                    "ignore_eos": True,
                    "temperature": 0,
                    "priority": "foreground",
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    capture.feed(chunk)
            capture.finish()
        finally:
            self.state.supervisor.request_finished()
        return {
            "duration_ms": (time.monotonic() - started) * 1000,
            "ttft_ms": capture.ttft_ms,
            "prompt_tokens": capture.prompt_tokens,
            "completion_tokens": capture.completion_tokens,
            "cached_tokens": capture.cached_tokens,
            "prompt_ms": capture.prompt_ms,
            "predicted_ms": capture.predicted_ms,
        }

    async def run(self, body: BenchmarkRequest, run: BenchmarkRun) -> None:
        total = sum(
            len(body.prefill_tokens)
            if s == "cold_prefill"
            else len(body.concurrency)
            if s == "concurrency"
            else 1
            for s in body.scenarios
        )
        step = 0
        try:
            async with httpx.AsyncClient(
                base_url=self.state.supervisor.base_url,
                headers={"Authorization": "Bearer " + self.state.supervisor.internal_key},
                timeout=600,
            ) as client:
                response = await client.post(
                    "/tokenize", json={"content": "int main() { return 42; }\n" * 100}
                )
                response.raise_for_status()
                ids = response.json()["tokens"]
                if not ids:
                    raise ValueError("Tokenizer returned no tokens")
                for scenario in body.scenarios:
                    values = (
                        body.prefill_tokens
                        if scenario == "cold_prefill"
                        else body.concurrency
                        if scenario == "concurrency"
                        else [32768 if scenario == "cached_ttft" else 128]
                    )
                    for value in values:
                        length = 128 if scenario == "concurrency" else value
                        prompt = (ids * (length // len(ids) + 1))[:length]
                        output_tokens = 512 if scenario in ("decode_short", "concurrency") else 1
                        self.state.events.publish(
                            "benchmark.progress",
                            BenchmarkProgressEvent(
                                run_id=run.id,
                                scenario=scenario,
                                step=step,
                                total_steps=total,
                                state="warmup",
                            ),
                        )
                        await self.sample(client, prompt, output_tokens)
                        samples: list[dict[str, Any]] = []
                        for _index in range(body.samples):
                            if scenario == "cold_prefill":
                                salt = await client.post(
                                    "/tokenize", json={"content": uuid.uuid4().hex}
                                )
                                salt.raise_for_status()
                                prefix = salt.json()["tokens"]
                                prompt = (prefix + prompt)[:length]
                            width = value if scenario == "concurrency" else 1
                            start = time.monotonic()
                            batch = await asyncio.gather(
                                *(self.sample(client, prompt, output_tokens) for _ in range(width))
                            )
                            duration = time.monotonic() - start
                            samples.append(
                                {
                                    "requests": batch,
                                    "aggregate_tps": sum(
                                        r.get("completion_tokens") or 0 for r in batch
                                    )
                                    / duration,
                                    "duration_ms": duration * 1000,
                                }
                            )
                        run.results.append(
                            BenchmarkResult(
                                scenario=scenario,
                                params={"value": value},
                                samples=samples,
                                summary={
                                    "aggregate_tps": statistics.mean(
                                        s["aggregate_tps"] for s in samples
                                    )
                                },
                            )
                        )
                        step += 1
                        self.state.usage.save_benchmark(run.model_dump())
                        self.state.events.publish(
                            "benchmark.progress",
                            BenchmarkProgressEvent(
                                run_id=run.id,
                                scenario=scenario,
                                step=step,
                                total_steps=total,
                                state="running",
                            ),
                        )
                run.state = "done"
        except asyncio.CancelledError:
            run.state = "cancelled"
        except Exception as error:
            run.state, run.error = "failed", str(error)
        finally:
            self.state.usage.save_benchmark(run.model_dump())
            self.state.events.publish(
                "benchmark.progress",
                BenchmarkProgressEvent(
                    run_id=run.id,
                    scenario="all",
                    step=step,
                    total_steps=total,
                    state=run.state,
                    message=run.error,
                ),
            )

    async def cancel(self) -> bool:
        if not self.task or self.task.done():
            return False
        self.task.cancel()
        await self.task
        return True

    async def shutdown(self) -> None:
        await self.cancel()


def create(state: ManagerState) -> Benchmark:
    service = Benchmark(state)
    state.benchmark = service
    return service

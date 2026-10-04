"""Repeatable benchmark runs with cancellation and persistent results (SPEC §10.6)."""

from __future__ import annotations

import asyncio
import contextlib
import statistics
import subprocess
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
from ..settings.effective import effective_serve
from ..system.info import system_info
from ..usage.db import iso, percentile

if TYPE_CHECKING:
    from ..state import ManagerState


# Apps whose GPU use skews results (SPEC §10.6 "other GPU-heavy apps running",
# best effort): matched case-insensitively against process names.
GPU_HEAVY_APPS = (
    "Final Cut Pro",
    "DaVinci Resolve",
    "Blender",
    "Motion",
    "Compressor",
    "Logic Pro",
    "LM Studio",
    "ollama",
    "llama-server",
    "mlx_lm",
    "Cinema 4D",
    "Unreal",
    "Unity",
)
# The /status.metrics counters a scenario reports as deltas (SPEC §10.6 Metrics).
STATUS_DELTAS = (
    "decode_output_tokens",
    "decode_cycle_ms",
    "decode_wall_ms",
    "prefill_input_tokens",
    "prefill_wall_ms",
    "drafted_tokens",
    "accepted_draft_tokens",
)


def running_gpu_apps(process_names: list[str] | None = None) -> list[str]:
    """Known GPU-heavy apps among the running processes (best effort, never raises)."""
    if process_names is None:
        try:
            out = subprocess.run(
                ["/bin/ps", "-axco", "comm="],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        process_names = out.splitlines()
    names = [n.strip().lower() for n in process_names]
    return [app for app in GPU_HEAVY_APPS if any(app.lower() in n for n in names)]


def _rate(tokens: Any, ms: Any) -> float | None:
    if isinstance(tokens, int | float) and isinstance(ms, int | float) and ms > 0:
        return float(tokens) * 1000.0 / float(ms)
    return None


def _mean(values: list[float | None]) -> float | None:
    data = [v for v in values if v is not None]
    return statistics.mean(data) if data else None


def summarize(
    scenario: str, samples: list[dict[str, Any]], delta: dict[str, Any]
) -> dict[str, Any]:
    """One scenario value's headline numbers from its samples' timings and the
    /status.metrics deltas over the samples."""
    requests = [r for s in samples for r in s["requests"]]
    ttfts = [r["ttft_ms"] for r in requests if r.get("ttft_ms") is not None]
    summary: dict[str, Any] = {
        "aggregate_tps": _mean([s["aggregate_tps"] for s in samples]),
        "decode_tps": _mean(
            [_rate(r.get("completion_tokens"), r.get("predicted_ms")) for r in requests]
        ),
        "prefill_tps": _mean([_rate(r.get("prompt_tokens"), r.get("prompt_ms")) for r in requests]),
        "ttft_p50_ms": percentile(ttfts, 0.5),
        "ttft_p95_ms": percentile(ttfts, 0.95),
        "cached_tokens": _mean([r.get("cached_tokens") for r in requests]),
        "prompt_tokens": _mean([r.get("prompt_tokens") for r in requests]),
    }
    drafted = delta.get("drafted_tokens")
    accepted = delta.get("accepted_draft_tokens")
    summary["status_delta"] = delta
    summary["status_decode_tps"] = _rate(
        delta.get("decode_output_tokens"), delta.get("decode_cycle_ms")
    )
    summary["draft_acceptance"] = (
        float(accepted) / float(drafted)
        if isinstance(drafted, int | float) and drafted and accepted is not None
        else None
    )
    return summary


def headline(results: list[dict[str, Any]]) -> dict[str, float | None]:
    """The few numbers the runs list and the compare view lead with."""
    out: dict[str, float | None] = {}
    for result in results:
        scenario, summary = result["scenario"], result.get("summary") or {}
        value = (result.get("params") or {}).get("value")
        if scenario == "decode_short":
            out["decode_tps"] = summary.get("decode_tps") or summary.get("aggregate_tps")
        elif scenario == "cold_prefill":
            out[f"prefill_tps_{value}"] = summary.get("prefill_tps")
        elif scenario == "cached_ttft":
            out["cached_ttft_ms"] = summary.get("ttft_p50_ms")
        elif scenario == "concurrency":
            best = out.get("concurrency_peak_tps")
            tps = summary.get("aggregate_tps")
            if tps is not None and (best is None or tps > best):
                out["concurrency_peak_tps"] = tps
    return out


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
        apps = running_gpu_apps()
        if apps:
            warnings.append("GPU-heavy apps are running: " + ", ".join(apps) + ".")
        if not self.state.supervisor.accepting:
            warnings.append("No model is loaded.")
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
        model = self.state.active_model() or ""
        revision = (self.state.model_info(model) or {}).get("revision") if model else None
        if revision is None and model:
            with contextlib.suppress(Exception):
                revision = effective_serve(self.state.settings.current, model).revision
        self.current = BenchmarkRun(
            id=uuid.uuid4().hex,
            ts=iso(),
            state="running",
            model=model,
            revision=revision,
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

    async def status_metrics(self, client: httpx.AsyncClient) -> dict[str, Any]:
        """The engine's /status.metrics counters now ({} when unavailable)."""
        try:
            response = await client.get("/status", timeout=10)
            metrics = response.json().get("metrics") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return {}
        return metrics if isinstance(metrics, dict) else {}

    @staticmethod
    def delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in STATUS_DELTAS:
            a, b = before.get(key), after.get(key)
            if isinstance(a, int | float) and isinstance(b, int | float) and b >= a:
                out[key] = b - a
        return out

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
                        before = await self.status_metrics(client)
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
                        after = await self.status_metrics(client)
                        run.results.append(
                            BenchmarkResult(
                                scenario=scenario,
                                params={
                                    "value": value,
                                    "prompt_tokens": length,
                                    "max_tokens": output_tokens,
                                    "width": value if scenario == "concurrency" else 1,
                                },
                                samples=samples,
                                summary=summarize(scenario, samples, self.delta(before, after)),
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

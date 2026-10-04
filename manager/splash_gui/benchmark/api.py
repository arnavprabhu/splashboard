"""Benchmarks (SPEC §14 Benchmark, §10.6)."""

from __future__ import annotations

from fastapi import APIRouter, Response

from ..errors import STUB_RESPONSES, error_responses, not_implemented
from ..schemas import (
    BenchmarkPreflight,
    BenchmarkRequest,
    BenchmarkRun,
    BenchmarkRuns,
    BenchmarkStarted,
    CancelResult,
)

router = APIRouter()
_ERR = {**STUB_RESPONSES, **error_responses(404)}


@router.post(
    "/benchmark",
    response_model=BenchmarkStarted,
    status_code=202,
    responses={**STUB_RESPONSES, **error_responses(409, 503)},
)
def start(body: BenchmarkRequest) -> BenchmarkStarted:
    not_implemented("Benchmark")


@router.get("/benchmark/preflight", response_model=BenchmarkPreflight, responses=STUB_RESPONSES)
def preflight() -> BenchmarkPreflight:
    not_implemented("Benchmark preflight")


@router.post("/benchmark/cancel", response_model=CancelResult, responses=STUB_RESPONSES)
def cancel() -> CancelResult:
    not_implemented("Benchmark")


@router.get("/benchmark/runs", response_model=BenchmarkRuns, responses=STUB_RESPONSES)
def runs() -> BenchmarkRuns:
    not_implemented("Benchmark runs")


@router.get("/benchmark/runs/{rid}", response_model=BenchmarkRun, responses=_ERR)
def run(rid: str) -> BenchmarkRun:
    not_implemented("Benchmark runs")


@router.delete("/benchmark/runs/{rid}", status_code=204, responses=_ERR)
def delete_run(rid: str) -> Response:
    not_implemented("Benchmark runs")

"""Benchmarks."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from ..errors import ApiError, error_responses
from ..schemas import (
    BenchmarkPreflight,
    BenchmarkRequest,
    BenchmarkRun,
    BenchmarkRuns,
    BenchmarkRunSummary,
    BenchmarkStarted,
    CancelResult,
)
from ..state import ManagerState, get_state
from .service import headline

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]
_ERR = error_responses(404)


@router.post(
    "/benchmark",
    response_model=BenchmarkStarted,
    status_code=202,
    responses=error_responses(409, 503),
)
async def start(state: State, body: BenchmarkRequest) -> BenchmarkStarted:
    return BenchmarkStarted.model_validate(state.benchmark.launch(body))


@router.post("/benchmark/preflight", response_model=BenchmarkPreflight, responses=_ERR)
def preflight(state: State) -> BenchmarkPreflight:
    return BenchmarkPreflight.model_validate(state.benchmark.preflight())


@router.post("/benchmark/cancel", response_model=CancelResult, responses=_ERR)
async def cancel(state: State) -> CancelResult:
    return CancelResult(cancelled=await state.benchmark.cancel())


@router.get("/benchmark/runs", response_model=BenchmarkRuns, responses=_ERR)
def runs(state: State) -> BenchmarkRuns:
    return BenchmarkRuns(
        runs=[
            BenchmarkRunSummary.model_validate({**r, "headline": headline(r["results"])})
            for r in state.usage.benchmarks()
        ]
    )


@router.get("/benchmark/runs/{rid}", response_model=BenchmarkRun, responses=_ERR)
def run(state: State, rid: str) -> BenchmarkRun:
    data = state.usage.benchmark(rid)
    if data is None:
        raise ApiError(404, "Benchmark not found", "benchmark_not_found")
    return BenchmarkRun.model_validate(data)


@router.delete("/benchmark/runs/{rid}", status_code=204, responses=_ERR)
def delete_run(state: State, rid: str) -> Response:
    data = state.usage.benchmark(rid)
    if data and data["state"] == "running":
        raise ApiError(409, "Cancel the benchmark before deleting it", "benchmark_running")
    if not state.usage.delete_benchmark(rid):
        raise ApiError(404, "Benchmark not found", "benchmark_not_found")
    return Response(status_code=204)

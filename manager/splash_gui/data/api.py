"""Data & privacy (SPEC §14 Data, §10.9)."""

from __future__ import annotations

from fastapi import APIRouter

from ..errors import STUB_RESPONSES, error_responses, not_implemented
from ..schemas import DataClearRequest, DataClearResult, DataSizes

router = APIRouter()


@router.get("/data/sizes", response_model=DataSizes, responses=STUB_RESPONSES)
def sizes() -> DataSizes:
    not_implemented("Data sizes")


@router.post(
    "/data/clear",
    response_model=DataClearResult,
    responses={**STUB_RESPONSES, **error_responses(409)},
)
def clear(body: DataClearRequest) -> DataClearResult:
    not_implemented("Data clear")

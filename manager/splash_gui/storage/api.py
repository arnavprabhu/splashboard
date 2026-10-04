"""Storage locations, moves and imports (SPEC §5, §10.2, §10.9)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ..errors import STUB_RESPONSES, error_responses, not_implemented
from ..paths import SPLASH_DATA_DIR
from ..schemas import ImportCandidates, ImportRequest, JobAccepted, StorageInfo, StorageMoveRequest
from ..state import ManagerState, get_state

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


@router.get("/storage", response_model=StorageInfo)
def storage(state: State) -> StorageInfo:
    """Locations now; byte counts are filled in by the models/storage track (null until then)."""
    return StorageInfo(
        models_dir=str(state.settings.models_dir()),
        cache_dir=str(state.settings.cache_dir()),
        tmp_dir=str(state.settings.tmp_dir()),
        splash_data_dir=str(SPLASH_DATA_DIR),
    )


@router.post(
    "/storage/move",
    response_model=JobAccepted,
    status_code=202,
    responses={**STUB_RESPONSES, **error_responses(400, 409, 507)},
)
def move(body: StorageMoveRequest) -> JobAccepted:
    not_implemented("Storage move")


@router.get("/storage/import-candidates", response_model=ImportCandidates, responses=STUB_RESPONSES)
def import_candidates() -> ImportCandidates:
    not_implemented("Import from Hugging Face cache")


@router.post(
    "/storage/import",
    response_model=JobAccepted,
    status_code=202,
    responses={**STUB_RESPONSES, **error_responses(400, 409)},
)
def import_models(body: ImportRequest) -> JobAccepted:
    not_implemented("Import from Hugging Face cache")

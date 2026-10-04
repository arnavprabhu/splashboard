"""Models: inventory, catalog, search, compatibility (SPEC §14 Models, §9).

Model IDs (`OWNER/REPO[:VARIANT]`) appear literally in paths. Routes with a
suffix after `{model_id:path}` must be declared before the bare one.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from ..errors import STUB_RESPONSES, error_responses, not_implemented
from ..schemas import (
    Catalog,
    DeleteModelResult,
    DownloadItem,
    InspectResult,
    InstalledModels,
    JobAccepted,
    ModelCard,
    ModelDetail,
    SearchResults,
    VerifyRequest,
)

router = APIRouter()
_ERR = {**STUB_RESPONSES, **error_responses(400, 404)}


@router.get("/models", response_model=InstalledModels, responses=STUB_RESPONSES)
def list_models() -> InstalledModels:
    not_implemented("Installed models")


@router.get("/catalog", response_model=Catalog, responses=STUB_RESPONSES)
def catalog() -> Catalog:
    not_implemented("Catalog")


@router.get("/search", response_model=SearchResults, responses=_ERR)
def search(
    q: Annotated[str, Query(min_length=1)],
    sort: Literal["downloads", "likes", "recent"] = "downloads",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> SearchResults:
    not_implemented("Hugging Face search")


@router.get("/inspect", response_model=InspectResult, responses=_ERR)
def inspect(id: Annotated[str, Query()], refresh: bool = False) -> InspectResult:
    not_implemented("Compatibility check")


@router.get("/card", response_model=ModelCard, responses=_ERR)
def card(id: Annotated[str, Query()]) -> ModelCard:
    not_implemented("Model card")


@router.post(
    "/models/{model_id:path}/verify", response_model=JobAccepted, status_code=202, responses=_ERR
)
def verify(model_id: str, body: VerifyRequest | None = None) -> JobAccepted:
    not_implemented("Verify")


@router.post(
    "/models/{model_id:path}/update",
    response_model=DownloadItem,
    status_code=202,
    responses={**_ERR, **error_responses(409)},
)
def update(model_id: str) -> DownloadItem:
    not_implemented("Model update")


@router.get("/models/{model_id:path}", response_model=ModelDetail, responses=_ERR)
def get_model(model_id: str) -> ModelDetail:
    not_implemented("Model detail")


@router.delete(
    "/models/{model_id:path}",
    response_model=DeleteModelResult,
    responses={**_ERR, **error_responses(409)},
)
def delete_model(model_id: str, confirm_active: bool = False) -> DeleteModelResult:
    not_implemented("Model delete")

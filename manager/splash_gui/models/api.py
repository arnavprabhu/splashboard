"""Models: inventory, catalog, search, compatibility (SPEC §14 Models, §9).

Model IDs (`OWNER/REPO[:VARIANT]`) appear literally in paths. Routes with a
suffix after `{model_id:path}` must be declared before the bare one.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Query

from ..downloads.service import Downloads
from ..errors import ApiError, error_responses
from ..schemas import (
    Catalog,
    DeleteModelResult,
    DownloadItem,
    DownloadRequest,
    InspectResult,
    InstalledModels,
    JobAccepted,
    ModelCard,
    ModelDetail,
    SearchResult,
    SearchResults,
    VerifyRequest,
)
from ..state import ManagerState, get_state
from .hf import HubError
from .service import Models

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]
_ERR = error_responses(400, 404, 503)


@router.get("/models", response_model=InstalledModels, responses=_ERR)
def list_models(state: State) -> InstalledModels:
    return cast(Models, state.models).inventory()


@router.get("/catalog", response_model=Catalog, responses=_ERR)
async def catalog(state: State, refresh: bool = False) -> Catalog:
    """The curated list filled from the Hub (cached a day; `refresh=true` refetches)."""
    return await cast(Models, state.models).catalog(refresh)


@router.get("/search", response_model=SearchResults, responses=_ERR)
async def search(
    state: State,
    q: Annotated[str, Query(min_length=1)],
    sort: Literal["downloads", "likes", "recent"] = "downloads",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> SearchResults:
    try:
        rows = await cast(Models, state.models).hf.search(q, sort, limit)
    except HubError as error:
        raise ApiError(error.status, error.message, "hub_unreachable") from None
    return SearchResults(
        query=q,
        sort=sort,
        results=[
            SearchResult(
                id=row.get("id") or row["modelId"],
                downloads=row.get("downloads"),
                likes=row.get("likes"),
                tags=row.get("tags") or [],
                last_modified=row.get("lastModified"),
                format_guess="gguf"
                if "gguf" in (row.get("tags") or [])
                else "mlx"
                if "mlx" in (row.get("tags") or [])
                else "unknown",
            )
            for row in rows
        ],
    )


@router.get("/inspect", response_model=InspectResult, responses=_ERR)
async def inspect(
    state: State, id: Annotated[str, Query()], refresh: bool = False
) -> InspectResult:
    return await cast(Models, state.models).inspect(id, refresh)


@router.get("/card", response_model=ModelCard, responses=_ERR)
async def card(state: State, id: Annotated[str, Query()]) -> ModelCard:
    return await cast(Models, state.models).card(id)


@router.post(
    "/models/{model_id:path}/verify", response_model=JobAccepted, status_code=202, responses=_ERR
)
async def verify(state: State, model_id: str, body: VerifyRequest | None = None) -> JobAccepted:
    return cast(Models, state.models).verify(model_id, body.full if body else False)


@router.post(
    "/models/{model_id:path}/update",
    response_model=DownloadItem,
    status_code=202,
    responses={**_ERR, **error_responses(409)},
)
async def update(state: State, model_id: str) -> DownloadItem:
    model = cast(Models, state.models).detail(model_id)
    if model.pinned:
        raise ApiError(409, "This model is pinned to a commit", "model_pinned")
    return await cast(Downloads, state.downloads).queue(
        DownloadRequest(id=model_id, language_only=model.language_only)
    )


@router.get("/models/{model_id:path}", response_model=ModelDetail, responses=_ERR)
def get_model(state: State, model_id: str) -> ModelDetail:
    return cast(Models, state.models).detail(model_id)


@router.delete(
    "/models/{model_id:path}",
    response_model=DeleteModelResult,
    responses={**_ERR, **error_responses(409)},
)
async def delete_model(
    state: State, model_id: str, confirm_active: bool = False
) -> DeleteModelResult:
    return await cast(Models, state.models).delete(model_id, confirm_active)

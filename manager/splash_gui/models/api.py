"""Models: inventory, catalog, search, compatibility.

Model IDs (`OWNER/REPO[:VARIANT]`) appear literally in paths. Routes with a
suffix after `{model_id:path}` must be declared before the bare one.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from ..downloads.service import Downloads
from ..errors import SSE_RESPONSES, ApiError, error_responses
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
    TokenPieces,
    TokenPiecesRequest,
    VerifyRequest,
)
from ..sse import sse_response
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


@router.post("/inspect", response_model=InspectResult, responses=_ERR)
async def inspect(
    state: State, id: Annotated[str, Query()], refresh: bool = False
) -> InspectResult:
    return await cast(Models, state.models).inspect(id, refresh)


@router.post("/inspect/stream", responses={**_ERR, **SSE_RESPONSES})
async def inspect_stream(
    state: State, id: Annotated[str, Query()], refresh: bool = False
) -> StreamingResponse:
    """D59: `/inspect` as server-sent events, POST like `/inspect` (D58: it runs
    Splash's helper, so a cross-site `<img>` or link must not trigger it).
    `inspect.progress` (a partial InspectResult, `pending` lists the variants still
    being checked) first and on every verdict, the likely recommended variant's
    first; then `inspect.result` (the complete InspectResult) or `inspect.error`.
    Hub and engine errors answer before the stream starts, with the same codes as
    `/inspect`."""
    return sse_response(await cast(Models, state.models).inspect_stream(id, refresh))


@router.get("/card", response_model=ModelCard, responses=_ERR)
async def card(state: State, id: Annotated[str, Query()]) -> ModelCard:
    return await cast(Models, state.models).card(id)


@router.get("/models/local")
def local_models(state: State) -> dict[str, Any]:
    """Loose `.gguf` files in the models folder and what became of each."""
    return cast(Models, state.models).local.view()


@router.post("/models/local/rescan", responses=error_responses(503))
async def rescan_local(state: State, restore_ignored: bool = False) -> dict[str, Any]:
    """Look for dropped `.gguf` files now; `restore_ignored` re-adds ones deleted in the GUI."""
    local = cast(Models, state.models).local
    added = await local.scan(restore_ignored=restore_ignored, retry=True)
    return {"added": added, **local.view()}


@router.post(
    "/models/{model_id:path}/verify",
    response_model=JobAccepted,
    status_code=202,
    responses={**_ERR, **error_responses(409)},
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
    state: State, model_id: str, confirm_active: bool = False, trash_source: bool = False
) -> DeleteModelResult:
    """`trash_source` (D67, `local/` models only): also move the model's `.gguf` to the Trash."""
    return await cast(Models, state.models).delete(model_id, confirm_active, trash_source)


@router.post(
    "/tokenizer/pieces", response_model=TokenPieces, responses=error_responses(404, 409, 503)
)
async def token_pieces(state: State, body: TokenPiecesRequest) -> TokenPieces:
    """Exact pieces for token ids from `/tokenize` (Tokenizer page)."""
    return await cast(Models, state.models).token_pieces(body.model, body.ids)

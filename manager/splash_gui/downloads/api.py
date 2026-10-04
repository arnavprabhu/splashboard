"""Download queue (SPEC §14 Downloads, §9.4)."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Response

from ..errors import error_responses
from ..schemas import DownloadItem, DownloadList, DownloadRequest
from ..state import ManagerState, get_state
from .service import Downloads

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]
_ERR = error_responses(404, 409)


@router.get("/downloads", response_model=DownloadList, responses=_ERR)
def list_downloads(state: State) -> DownloadList:
    return DownloadList(
        items=list(cast(Downloads, state.downloads).items.values()),
        parallel=state.settings.current.global_.downloads.parallel,
    )


@router.post(
    "/downloads",
    response_model=DownloadItem,
    status_code=201,
    responses=error_responses(400, 409, 422, 507),
)
async def queue_download(state: State, body: DownloadRequest) -> DownloadItem:
    return await cast(Downloads, state.downloads).queue(body)


@router.post("/downloads/{dl}/pause", response_model=DownloadItem, responses=_ERR)
async def pause(state: State, dl: str) -> DownloadItem:
    return await cast(Downloads, state.downloads).pause(dl)


@router.post("/downloads/{dl}/resume", response_model=DownloadItem, responses=_ERR)
async def resume(state: State, dl: str) -> DownloadItem:
    return cast(Downloads, state.downloads).resume(dl)


@router.delete("/downloads/{dl}", status_code=204, responses=_ERR)
async def cancel(state: State, dl: str, keep_files: bool = False) -> Response:
    await cast(Downloads, state.downloads).cancel(dl, keep_files)
    return Response(status_code=204)

"""Download queue (SPEC §14 Downloads, §9.4)."""

from __future__ import annotations

from fastapi import APIRouter, Response

from ..errors import STUB_RESPONSES, error_responses, not_implemented
from ..schemas import DownloadItem, DownloadList, DownloadRequest

router = APIRouter()
_ERR = {**STUB_RESPONSES, **error_responses(404, 409)}


@router.get("/downloads", response_model=DownloadList, responses=STUB_RESPONSES)
def list_downloads() -> DownloadList:
    not_implemented("Downloads")


@router.post(
    "/downloads",
    response_model=DownloadItem,
    status_code=201,
    responses={**STUB_RESPONSES, **error_responses(400, 409, 422, 507)},
)
def queue_download(body: DownloadRequest) -> DownloadItem:
    not_implemented("Downloads")


@router.post("/downloads/{dl}/pause", response_model=DownloadItem, responses=_ERR)
def pause(dl: str) -> DownloadItem:
    not_implemented("Downloads")


@router.post("/downloads/{dl}/resume", response_model=DownloadItem, responses=_ERR)
def resume(dl: str) -> DownloadItem:
    not_implemented("Downloads")


@router.delete("/downloads/{dl}", status_code=204, responses=_ERR)
def cancel(dl: str, keep_files: bool = False) -> Response:
    not_implemented("Downloads")

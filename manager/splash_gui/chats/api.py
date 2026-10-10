"""Chat history."""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse

from ..errors import error_responses
from ..schemas import AttachmentUpload, Chat, ChatCreate, ChatList, DeleteCount
from ..state import ManagerState, get_state
from .store import MEDIA_TYPES, ChatStore

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]
_ERR = error_responses(404, 422)
_UPLOAD = {
    "requestBody": {
        "content": {
            "image/*": {"schema": {"type": "string", "format": "binary"}},
            "application/pdf": {"schema": {"type": "string", "format": "binary"}},
        }
    }
}


def store(state: ManagerState) -> ChatStore:
    chats = state.chats
    if chats is None:  # the service didn't start (no lifespan in a test): build on demand
        chats = ChatStore(state.paths.chats_dir, state.usage)
        state.chats = chats
    assert isinstance(chats, ChatStore)
    return chats


@router.get("/chats", response_model=ChatList)
def list_chats(state: State, q: Annotated[str | None, Query()] = None) -> ChatList:
    return ChatList(chats=store(state).summaries(q))


@router.post("/chats", response_model=Chat, status_code=201)
def create_chat(state: State, body: ChatCreate) -> Chat:
    return store(state).create(body)


@router.delete("/chats", response_model=DeleteCount)
def delete_all_chats(state: State) -> DeleteCount:
    return DeleteCount(deleted=store(state).delete_all())


async def _upload(
    state: ManagerState, request: Request, chat_id: str | None = None
) -> AttachmentUpload:
    from ..errors import ApiError

    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > 64 * 1024 * 1024:
        raise ApiError(413, "attachments are limited to 64 MiB", "attachment_too_large")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 64 * 1024 * 1024:
            raise ApiError(413, "attachments are limited to 64 MiB", "attachment_too_large")
    return store(state).add_attachment(
        bytes(data), request.headers.get("content-type", ""), chat_id
    )


@router.post(
    "/chats/attachments",
    response_model=AttachmentUpload,
    status_code=201,
    responses=error_responses(400, 413, 415),
    openapi_extra=_UPLOAD,
)
async def upload_attachment(state: State, request: Request) -> AttachmentUpload:
    """Store an image or PDF once by sha256; messages reference `file`."""
    return await _upload(state, request)


def _file(state: ManagerState, name: str) -> FileResponse:
    path = store(state).attachment_path(name)
    media = MEDIA_TYPES.get(path.suffix.lstrip("."), "application/octet-stream")
    return FileResponse(
        path, media_type=media, headers={"Cache-Control": "private, max-age=31536000, immutable"}
    )


@router.get("/chats/attachments/{name}", response_class=Response, responses=_ERR)
def get_attachment(state: State, name: str) -> FileResponse:
    return _file(state, name)


@router.post(
    "/chats/{cid}/attachments",
    response_model=AttachmentUpload,
    status_code=201,
    responses=error_responses(400, 404, 413, 415),
    openapi_extra=_UPLOAD,
)
async def upload_chat_attachment(state: State, cid: str, request: Request) -> AttachmentUpload:
    """Same as `POST /chats/attachments` (the per-chat path); the chat must exist."""
    store(state).get(cid)
    return await _upload(state, request, cid)


@router.get("/chats/{cid}/attachments/{name}", response_class=Response, responses=_ERR)
def get_chat_attachment(state: State, cid: str, name: str) -> FileResponse:
    store(state).get(cid)
    return _file(state, name)


@router.get("/chats/{cid}", response_model=Chat, responses=_ERR)
def get_chat(state: State, cid: str) -> Chat:
    return store(state).get(cid)


@router.put("/chats/{cid}", response_model=Chat, responses=_ERR)
def put_chat(state: State, cid: str, body: Chat) -> Chat:
    return store(state).put(cid, body)


@router.delete("/chats/{cid}", status_code=204, responses=_ERR)
def delete_chat(state: State, cid: str) -> Response:
    store(state).delete(cid)
    return Response(status_code=204)


def _filename(title: str, ext: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-")[:60] or "chat"
    return f"{slug}.{ext}"


@router.get(
    "/chats/{cid}/export",
    response_class=Response,
    responses={
        200: {"content": {"application/json": {}, "text/markdown": {}}},
        **error_responses(404),
    },
)
def export_chat(state: State, cid: str, format: Literal["json", "md"] = "json") -> Response:
    chats = store(state)
    chat = chats.get(cid)
    if format == "md":
        body = chats.markdown(chat)
        media = "text/markdown; charset=utf-8"
    else:
        body = json.dumps(chat.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        media = "application/json"
    return Response(
        body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{_filename(chat.title, format)}"'},
    )

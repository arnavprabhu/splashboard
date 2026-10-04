"""Chat history (SPEC §14 Chats, §15.2)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response

from ..errors import STUB_RESPONSES, error_responses, not_implemented
from ..schemas import AttachmentUpload, Chat, ChatCreate, ChatList, DeleteCount

router = APIRouter()
_ERR = {**STUB_RESPONSES, **error_responses(404)}


@router.get("/chats", response_model=ChatList, responses=STUB_RESPONSES)
def list_chats(q: Annotated[str | None, Query()] = None) -> ChatList:
    not_implemented("Chats")


@router.post("/chats", response_model=Chat, status_code=201, responses=STUB_RESPONSES)
def create_chat(body: ChatCreate) -> Chat:
    not_implemented("Chats")


@router.delete("/chats", response_model=DeleteCount, responses=STUB_RESPONSES)
def delete_all_chats() -> DeleteCount:
    not_implemented("Chats")


@router.post(
    "/chats/attachments",
    response_model=AttachmentUpload,
    status_code=201,
    responses={**STUB_RESPONSES, **error_responses(413, 415)},
    openapi_extra={
        "requestBody": {
            "content": {
                "image/*": {"schema": {"type": "string", "format": "binary"}},
                "application/pdf": {"schema": {"type": "string", "format": "binary"}},
            }
        }
    },
)
def upload_attachment() -> AttachmentUpload:
    not_implemented("Chat attachments")


@router.get("/chats/attachments/{name}", response_class=Response, responses=_ERR)
def get_attachment(name: str) -> Response:
    not_implemented("Chat attachments")


@router.get("/chats/{cid}", response_model=Chat, responses=_ERR)
def get_chat(cid: str) -> Chat:
    not_implemented("Chats")


@router.put("/chats/{cid}", response_model=Chat, responses=_ERR)
def put_chat(cid: str, body: Chat) -> Chat:
    not_implemented("Chats")


@router.delete("/chats/{cid}", status_code=204, responses=_ERR)
def delete_chat(cid: str) -> Response:
    not_implemented("Chats")


@router.get("/chats/{cid}/export", response_class=Response, responses=_ERR)
def export_chat(cid: str, format: Literal["json", "md"] = "json") -> Response:
    not_implemented("Chat export")

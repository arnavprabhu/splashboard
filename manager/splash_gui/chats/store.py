"""Chat history on disk (SPEC §10.5, §15.2).

One `~/.splash/chats/<uuid>.json` per conversation (atomic writes, 0600), messages
forming a tree through `parent`; attachments stored once by sha256 under
`chats/attachments/`; a full-text index in usage.db's FTS5 table `chat_fts`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..errors import ApiError
from ..paths import FILE_MODE, ensure_private_dir, write_atomic
from ..schemas import AttachmentUpload, Chat, ChatCreate, ChatMessage, ChatSummary
from ..usage.db import UsageDB

MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ATTACHMENT = re.compile(r"[0-9a-f]{64}\.[a-z0-9]{1,8}")
IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
}
MEDIA_TYPES = {v: k for k, v in IMAGE_TYPES.items() if k != "image/jpg"} | {
    "pdf": "application/pdf"
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def message_text(message: ChatMessage) -> str:
    """The searchable text of a message: string content or its text parts."""
    content = message.content
    if isinstance(content, str):
        parts = [content]
    else:
        parts = [
            str(part.get("text"))
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
    if message.reasoning:
        parts.append(message.reasoning)
    return "\n".join(p for p in parts if p)


def has_data_url(value: Any) -> bool:
    """Whether any string inside `value` is a data: URL (UI gap G2)."""
    if isinstance(value, str):
        return value.lstrip().lower().startswith("data:")
    if isinstance(value, dict):
        return any(has_data_url(v) for v in value.values())
    if isinstance(value, list):
        return any(has_data_url(v) for v in value)
    return False


def validate_tree(chat: Chat) -> None:
    ids: set[str] = set()
    for message in chat.messages:
        if message.id in ids:
            raise ApiError(422, f"duplicate message id {message.id}", "invalid_chat")
        ids.add(message.id)
    for message in chat.messages:
        if message.parent is not None and message.parent not in ids:
            raise ApiError(
                422,
                f"message {message.id} names an unknown parent {message.parent}",
                "invalid_chat",
            )
        if message.parent == message.id:
            raise ApiError(422, f"message {message.id} is its own parent", "invalid_chat")
    if chat.active_leaf is not None and chat.active_leaf not in ids:
        raise ApiError(422, "active_leaf must be a message id", "invalid_chat")
    # Cycles: walk each message up to the root.
    parents = {m.id: m.parent for m in chat.messages}
    for start in parents:
        seen: set[str] = set()
        node: str | None = start
        while node is not None:
            if node in seen:
                raise ApiError(422, "messages form a cycle", "invalid_chat")
            seen.add(node)
            node = parents.get(node)
    for message in chat.messages:
        for attachment in message.attachments:
            name = attachment.file.removeprefix("attachments/")
            if not attachment.file.startswith("attachments/") or not _ATTACHMENT.fullmatch(name):
                raise ApiError(422, f"invalid attachment path {attachment.file}", "invalid_chat")
    for message in chat.messages:
        if has_data_url(message.content):
            raise ApiError(
                422,
                "messages must reference uploaded attachments, not embed data: URLs",
                "invalid_chat",
            )


class ChatStore:
    def __init__(self, directory: Path, usage: UsageDB) -> None:
        self.directory = directory
        self.attachments = directory / "attachments"
        self.usage = usage
        self._lock = threading.RLock()
        ensure_private_dir(directory)
        ensure_private_dir(self.attachments)

    async def start(self) -> None:
        self.reindex()

    async def shutdown(self) -> None:
        return None

    # Files -------------------------------------------------------------------------

    def _path(self, chat_id: str) -> Path:
        if not _ID.fullmatch(chat_id):
            raise ApiError(404, f"no chat {chat_id}", "chat_not_found")
        return self.directory / f"{chat_id}.json"

    def _read(self, path: Path) -> Chat | None:
        try:
            return Chat.model_validate_json(path.read_bytes())
        except (OSError, ValidationError, ValueError):
            return None

    def _write(self, chat: Chat) -> None:
        data = json.dumps(chat.model_dump(mode="json"), ensure_ascii=False, indent=1).encode()
        write_atomic(self._path(chat.id), data, FILE_MODE)
        self.usage.index_chat(chat.id, chat.title, self._body(chat))

    @staticmethod
    def _body(chat: Chat) -> str:
        parts = [chat.system or ""]
        parts += [message_text(m) for m in chat.messages]
        return "\n".join(p for p in parts if p)

    def all(self) -> list[Chat]:
        chats = []
        with self._lock:
            for path in self.directory.glob("*.json"):
                chat = self._read(path)
                if chat is not None:
                    chats.append(chat)
        return sorted(chats, key=lambda c: c.updated_at, reverse=True)

    def reindex(self) -> None:
        self.usage.unindex_chat()
        for chat in self.all():
            self.usage.index_chat(chat.id, chat.title, self._body(chat))

    # CRUD --------------------------------------------------------------------------

    def get(self, chat_id: str) -> Chat:
        chat = self._read(self._path(chat_id))
        if chat is None:
            raise ApiError(404, f"no chat {chat_id}", "chat_not_found")
        return chat

    def create(self, body: ChatCreate) -> Chat:
        now = now_iso()
        chat = Chat(
            id=uuid.uuid4().hex,
            title=(body.title or "New chat").strip() or "New chat",
            created_at=now,
            updated_at=now,
            model=body.model,
            profile=body.profile or "default",
            system=body.system,
            sampling=body.sampling or {},
            tools=body.tools or [],
            response_format=body.response_format,
        )
        with self._lock:
            self._write(chat)
        return chat

    def put(self, chat_id: str, chat: Chat) -> Chat:
        if chat.id != chat_id:
            raise ApiError(422, "the chat id in the body must match the path", "invalid_chat")
        validate_tree(chat)
        with self._lock:
            existing = self._read(self._path(chat_id))
            created = existing.created_at if existing else chat.created_at
            saved = chat.model_copy(update={"created_at": created, "updated_at": now_iso()})
            self._write(saved)
        return saved

    def delete(self, chat_id: str) -> None:
        path = self._path(chat_id)
        with self._lock:
            if not path.exists():
                raise ApiError(404, f"no chat {chat_id}", "chat_not_found")
            path.unlink()
            self.usage.unindex_chat(chat_id)
            self._collect_attachments()

    def delete_all(self) -> int:
        with self._lock:
            count = 0
            for path in self.directory.glob("*.json"):
                path.unlink()
                count += 1
            for path in self.attachments.iterdir():
                with contextlib.suppress(OSError):
                    path.unlink()
            self.usage.unindex_chat()
        return count

    def summaries(self, query: str | None) -> list[ChatSummary]:
        chats = self.all()
        snippets: dict[str, str | None] = {}
        if query and query.strip():
            hits = self.usage.search_chats(query)
            snippets = dict(hits)
            if not self.usage.fts:  # no FTS5: substring match
                lowered = query.lower()
                snippets = {
                    c.id: None
                    for c in chats
                    if lowered in c.title.lower() or lowered in self._body(c).lower()
                }
            chats = [c for c in chats if c.id in snippets]
        return [
            ChatSummary(
                id=c.id,
                title=c.title,
                created_at=c.created_at,
                updated_at=c.updated_at,
                model=c.model,
                profile=c.profile,
                message_count=len(c.messages),
                snippet=snippets.get(c.id),
            )
            for c in chats
        ]

    # Attachments -----------------------------------------------------------------------

    def add_attachment(self, data: bytes, content_type: str) -> AttachmentUpload:
        media = content_type.split(";", 1)[0].strip().lower()
        if media == "application/pdf":
            ext, kind = "pdf", "pdf"
        elif media in IMAGE_TYPES:
            ext, kind = IMAGE_TYPES[media], "image"
        elif media.startswith("image/"):
            ext, kind = re.sub(r"[^a-z0-9]", "", media.split("/", 1)[1])[:8] or "img", "image"
        else:
            raise ApiError(415, "attachments must be images or PDFs", "unsupported_media_type")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ApiError(413, "attachments are limited to 64 MiB", "attachment_too_large")
        if not data:
            raise ApiError(400, "the attachment is empty", "invalid_request")
        digest = hashlib.sha256(data).hexdigest()
        name = f"{digest}.{ext}"
        path = self.attachments / name
        if not path.exists():
            write_atomic(path, data, FILE_MODE)
        return AttachmentUpload(
            file=f"attachments/{name}",
            sha256=digest,
            bytes=len(data),
            kind=kind,  # type: ignore[arg-type]
        )

    def attachment_path(self, name: str) -> Path:
        name = name.removeprefix("attachments/")
        if not _ATTACHMENT.fullmatch(name):
            raise ApiError(404, f"no attachment {name}", "attachment_not_found")
        path = self.attachments / name
        if not path.is_file():
            raise ApiError(404, f"no attachment {name}", "attachment_not_found")
        return path

    def _collect_attachments(self) -> None:
        """Delete attachments no chat references any more."""
        used = {
            a.file.removeprefix("attachments/")
            for chat in self.all()
            for message in chat.messages
            for a in message.attachments
        }
        for path in self.attachments.iterdir():
            if path.name not in used and _ATTACHMENT.fullmatch(path.name):
                with contextlib.suppress(OSError):
                    path.unlink()

    def size(self) -> tuple[int, int]:
        """(bytes, chat count) of the chat store, attachments included."""
        total = 0
        count = 0
        for path in self.directory.rglob("*"):
            if path.is_file():
                with contextlib.suppress(OSError):
                    total += path.stat().st_size
                if path.parent == self.directory and path.suffix == ".json":
                    count += 1
        return total, count

    # Export --------------------------------------------------------------------------

    @staticmethod
    def active_path(chat: Chat) -> list[ChatMessage]:
        """The messages on the branch ending at `active_leaf` (else every message)."""
        if chat.active_leaf is None:
            return list(chat.messages)
        by_id = {m.id: m for m in chat.messages}
        path: list[ChatMessage] = []
        node: str | None = chat.active_leaf
        while node is not None and node in by_id:
            path.append(by_id[node])
            node = by_id[node].parent
        return list(reversed(path))

    def markdown(self, chat: Chat) -> str:
        lines = [f"# {chat.title}", ""]
        meta = [
            f"Model: {chat.model or '—'}",
            f"Profile: {chat.profile}",
            f"Created: {chat.created_at}",
        ]
        lines += [" · ".join(meta), ""]
        if chat.system:
            lines += ["## System", "", chat.system, ""]
        labels = {"user": "You", "assistant": "Assistant", "tool": "Tool", "system": "System"}
        for message in self.active_path(chat):
            lines.append(f"## {labels[message.role]}")
            lines.append("")
            if message.reasoning:
                quoted = "\n".join(f"> {line}" for line in message.reasoning.splitlines())
                lines += ["> **Thinking**", ">", quoted, ""]
            text = message_text(message.model_copy(update={"reasoning": None}))
            if text:
                lines += [text, ""]
            for call in message.tool_calls or []:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict):
                    lines += [
                        f"Tool call `{function.get('name')}`:",
                        "",
                        "```json",
                        str(function.get("arguments", "")),
                        "```",
                        "",
                    ]
            for attachment in message.attachments:
                label = attachment.name or attachment.file.removeprefix("attachments/")
                lines.append(f"- Attachment ({attachment.kind}): {label} — `{attachment.file}`")
            if message.attachments:
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"

"""Chat history on disk.

One `~/.splash/chats/<uuid>.json` per conversation (atomic writes, 0600), messages
forming a tree through `parent`; attachments stored once by sha256 under
`chats/attachments/`; a full-text index in usage.db's FTS5 table `chat_fts`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import threading
import time
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
# An upload no saved message references yet is left alone this long when another
# chat is deleted: the browser uploads first and saves the message a moment later.
ORPHAN_GRACE_SECONDS = 24 * 60 * 60
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
    """Whether any string inside `value` is a data: URL."""
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
        # Pending uploads, kept in `.pending-uploads` so they outlive a
        # restart. Attachment name -> chat it was uploaded for -> upload time.
        self._uploads: dict[str, dict[str, float]] = {}
        # Attachment name -> time of an upload made without a chat that no saved
        # message has picked up yet.
        self._loose: dict[str, float] = {}
        ensure_private_dir(directory)
        ensure_private_dir(self.attachments)
        self._load_pending()

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
    def _files(chat: Chat | None) -> set[str]:
        """Attachment names the chat's messages use."""
        if chat is None:
            return set()
        return {a.file.removeprefix("attachments/") for m in chat.messages for a in m.attachments}

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
        path = self._path(chat_id)
        with self._lock:
            # Conversations are created with POST; a late save must not bring a deleted one back.
            if not path.exists():
                raise ApiError(404, f"no chat {chat_id}", "chat_not_found")
            existing = self._read(path)
            created = existing.created_at if existing else chat.created_at
            saved = chat.model_copy(update={"created_at": created, "updated_at": now_iso()})
            self._write(saved)
            # Uploads this save starts using are no longer pending.
            changed = False
            for name in self._files(saved) - self._files(existing):
                changed |= self._loose.pop(name, None) is not None
                chats = self._uploads.get(name, {})
                changed |= chats.pop(chat_id, None) is not None
                if not chats:
                    self._uploads.pop(name, None)
            if changed:
                self._save_pending()
        return saved

    def delete(self, chat_id: str) -> None:
        path = self._path(chat_id)
        with self._lock:
            if not path.exists():
                raise ApiError(404, f"no chat {chat_id}", "chat_not_found")
            own = self._files(self._read(path))
            own |= {name for name, chats in self._uploads.items() if chat_id in chats}
            path.unlink()
            self.usage.unindex_chat(chat_id)
            self._collect_attachments(chat_id, own)

    def delete_all(self) -> int:
        with self._lock:
            count = 0
            for path in self.directory.glob("*.json"):
                path.unlink()
                count += 1
            for path in self.attachments.iterdir():
                with contextlib.suppress(OSError):
                    path.unlink()
            self._uploads.clear()
            self._loose.clear()
            self._save_pending()
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

    def add_attachment(
        self, data: bytes, content_type: str, chat_id: str | None = None
    ) -> AttachmentUpload:
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
        with self._lock:
            if path.exists():
                # A fresh upload of known bytes restarts the grace period.
                with contextlib.suppress(OSError):
                    path.touch()
            else:
                write_atomic(path, data, FILE_MODE)
            if chat_id is not None:
                self._uploads.setdefault(name, {})[chat_id] = time.time()
            else:
                self._loose[name] = time.time()
            self._save_pending()
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

    def _collect_attachments(self, deleted: str, own: set[str]) -> None:
        """Delete attachments no chat references any more, after chat `deleted` went.

        Files are shared by content, so an unreferenced file stays while another
        chat's upload of it is pending (uploaded for that chat, or uploaded without
        a chat within the grace period, and not saved yet). Otherwise it goes when
        the deleted chat used or uploaded it, or when it is older than the grace
        period.
        """
        files = {chat.id: self._files(chat) for chat in self.all()}
        used = {name for names in files.values() for name in names}
        cutoff = time.time() - ORPHAN_GRACE_SECONDS
        self._prune_pending(files, cutoff)
        self._save_pending()
        for path in self.attachments.iterdir():
            name = path.name
            if name in used or not _ATTACHMENT.fullmatch(name):
                continue
            if name in self._uploads or name in self._loose:
                continue
            if name not in own:
                try:
                    if path.stat().st_mtime > cutoff:
                        continue
                except OSError:
                    continue
            with contextlib.suppress(OSError):
                path.unlink()

    def _prune_pending(self, files: dict[str, set[str]], cutoff: float) -> None:
        """Drop pending uploads past the grace, whose file is gone, or whose chat is
        gone or already references them. `files` maps each chat to the files it uses.
        A chatless upload stays until a save picks it up or the grace ends, since it
        can't tell which chat it is meant for."""

        def stale(name: str, at: float) -> bool:
            return at <= cutoff or not (self.attachments / name).exists()

        for name in list(self._uploads):
            chats = {
                c: at
                for c, at in self._uploads[name].items()
                if not stale(name, at) and c in files and name not in files[c]
            }
            if chats:
                self._uploads[name] = chats
            else:
                del self._uploads[name]
        for name, at in list(self._loose.items()):
            if stale(name, at):
                del self._loose[name]

    def _load_pending(self) -> None:
        """Read the pending uploads saved before a restart; a missing or damaged
        file leaves only the file-age grace to protect them."""
        try:
            raw = json.loads(self._pending_path.read_bytes())
            uploads = raw.get("uploads", {})
            loose = raw.get("loose", {})
            for name, chats in uploads.items():
                if not _ATTACHMENT.fullmatch(name):
                    continue
                kept = {
                    c: float(at)
                    for c, at in chats.items()
                    if _ID.fullmatch(c) and isinstance(at, int | float) and math.isfinite(at)
                }
                if kept:
                    self._uploads[name] = kept
            for name, at in loose.items():
                if not (_ATTACHMENT.fullmatch(name) and isinstance(at, int | float)):
                    continue
                if math.isfinite(at):
                    self._loose[name] = float(at)
        except (OSError, ValueError, AttributeError, TypeError, OverflowError):
            self._uploads.clear()
            self._loose.clear()
            return
        files = {chat.id: self._files(chat) for chat in self.all()}
        self._prune_pending(files, time.time() - ORPHAN_GRACE_SECONDS)
        self._save_pending()

    @property
    def _pending_path(self) -> Path:
        # Beside the chat files, not named *.json so it is never read as a chat.
        return self.directory / ".pending-uploads"

    def _save_pending(self) -> None:
        """Write the pending uploads (or remove the file when there are none). Best
        effort: without the file, the file-age grace still protects recent uploads."""
        with contextlib.suppress(OSError):
            if not self._uploads and not self._loose:
                self._pending_path.unlink(missing_ok=True)
                return
            data = {"uploads": self._uploads, "loose": self._loose}
            write_atomic(self._pending_path, json.dumps(data).encode(), FILE_MODE)

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

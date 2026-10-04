"""Chat history: CRUD, branches, attachments, search, export (SPEC §10.5, §15.2)."""

from __future__ import annotations

import hashlib
import stat
from typing import Any

from fastapi.testclient import TestClient

from splash_gui.paths import Paths

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def _chat(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/api/admin/chats", json={"title": "Dates", **body})
    assert response.status_code == 201, response.text
    return dict(response.json())


def _msg(mid: str, parent: str | None, role: str, content: Any, **extra: Any) -> dict[str, Any]:
    return {"id": mid, "parent": parent, "role": role, "content": content, **extra}


def test_create_get_put_delete(client: TestClient, paths: Paths) -> None:
    chat = _chat(client, model="m/x", system="be brief")
    assert chat["profile"] == "default" and chat["messages"] == []
    file = paths.chats_dir / f"{chat['id']}.json"
    assert file.exists() and stat.S_IMODE(file.stat().st_mode) == 0o600
    chat["messages"] = [
        _msg("u1", None, "user", "parse 2026-10-03 please"),
        _msg(
            "a1",
            "u1",
            "assistant",
            "Use date.fromisoformat",
            reasoning="easy",
            meta={"usage": {"prompt_tokens": 9}, "ttft_ms": 31.5, "extra": 1},
        ),
        _msg("a2", "u1", "assistant", "Or strptime"),
    ]
    chat["active_leaf"] = "a2"
    saved = client.put(f"/api/admin/chats/{chat['id']}", json=chat)
    assert saved.status_code == 200, saved.text
    assert saved.json()["created_at"] == chat["created_at"]
    got = client.get(f"/api/admin/chats/{chat['id']}").json()
    assert got["messages"][1]["meta"]["extra"] == 1 and got["active_leaf"] == "a2"
    listed = client.get("/api/admin/chats").json()["chats"]
    assert listed[0]["id"] == chat["id"] and listed[0]["message_count"] == 3
    assert client.delete(f"/api/admin/chats/{chat['id']}").status_code == 204
    assert client.get(f"/api/admin/chats/{chat['id']}").json()["error"]["code"] == "chat_not_found"


def test_branch_validation(client: TestClient) -> None:
    chat = _chat(client)
    url = f"/api/admin/chats/{chat['id']}"
    bad_parent = {**chat, "messages": [_msg("a", "missing", "user", "hi")]}
    assert client.put(url, json=bad_parent).status_code == 422
    bad_leaf = {**chat, "messages": [_msg("a", None, "user", "hi")], "active_leaf": "zz"}
    assert client.put(url, json=bad_leaf).status_code == 422
    cycle = {**chat, "messages": [_msg("a", "b", "user", "x"), _msg("b", "a", "user", "y")]}
    assert client.put(url, json=cycle).json()["error"]["code"] == "invalid_chat"
    other_id = {**chat, "id": "nope"}
    assert client.put(url, json=other_id).status_code == 422


def test_data_urls_are_refused(client: TestClient) -> None:
    chat = _chat(client)
    part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    body = {**chat, "messages": [_msg("u", None, "user", [part])]}
    response = client.put(f"/api/admin/chats/{chat['id']}", json=body)
    assert response.status_code == 422 and "data:" in response.json()["error"]["message"]


def test_attachments_upload_dedupe_download(client: TestClient, paths: Paths) -> None:
    up = client.post(
        "/api/admin/chats/attachments", content=PNG, headers={"Content-Type": "image/png"}
    )
    assert up.status_code == 201, up.text
    body = up.json()
    digest = hashlib.sha256(PNG).hexdigest()
    assert body == {
        "file": f"attachments/{digest}.png",
        "sha256": digest,
        "bytes": len(PNG),
        "kind": "image",
    }
    again = client.post(
        "/api/admin/chats/attachments", content=PNG, headers={"Content-Type": "image/png"}
    )
    assert again.json() == body
    assert len(list(paths.attachments_dir.iterdir())) == 1
    got = client.get(f"/api/admin/chats/attachments/{digest}.png")
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["content-type"] == "image/png"
    pdf = client.post(
        "/api/admin/chats/attachments",
        content=b"%PDF-1.7",
        headers={"Content-Type": "application/pdf"},
    )
    assert pdf.json()["kind"] == "pdf"
    refused = client.post(
        "/api/admin/chats/attachments", content=b"x", headers={"Content-Type": "text/plain"}
    )
    assert refused.status_code == 415
    assert client.get("/api/admin/chats/attachments/../../settings.json").status_code == 404
    chat = _chat(client)
    per_chat = client.post(
        f"/api/admin/chats/{chat['id']}/attachments",
        content=PNG,
        headers={"Content-Type": "image/png"},
    )
    assert per_chat.json() == body
    assert client.get(f"/api/admin/chats/{chat['id']}/attachments/{digest}.png").content == PNG


def test_attachment_size_limit(client: TestClient) -> None:
    response = client.post(
        "/api/admin/chats/attachments",
        content=b"",
        headers={"Content-Type": "image/png", "Content-Length": str(65 * 1024 * 1024)},
    )
    assert response.status_code == 413


def test_unreferenced_attachments_are_collected_on_delete(client: TestClient, paths: Paths) -> None:
    up = client.post(
        "/api/admin/chats/attachments", content=PNG, headers={"Content-Type": "image/png"}
    ).json()
    chat = _chat(client)
    chat["messages"] = [
        _msg("u", None, "user", "look", attachments=[{"kind": "image", "file": up["file"]}])
    ]
    assert client.put(f"/api/admin/chats/{chat['id']}", json=chat).status_code == 200
    client.delete(f"/api/admin/chats/{chat['id']}")
    assert list(paths.attachments_dir.iterdir()) == []


def test_search_titles_and_content(client: TestClient) -> None:
    a = _chat(client, title="Weather in Oslo")
    b = _chat(client, title="Something else")
    b["messages"] = [_msg("u", None, "user", [{"type": "text", "text": "how do tokenizers work"}])]
    client.put(f"/api/admin/chats/{b['id']}", json=b)
    hits = client.get("/api/admin/chats", params={"q": "oslo"}).json()["chats"]
    assert [c["id"] for c in hits] == [a["id"]]
    hits = client.get("/api/admin/chats", params={"q": "tokeniz"}).json()["chats"]
    assert [c["id"] for c in hits] == [b["id"]] and "tokenizers" in (hits[0]["snippet"] or "")
    assert client.get("/api/admin/chats", params={"q": "zzzz"}).json()["chats"] == []


def test_export_json_and_markdown(client: TestClient) -> None:
    chat = _chat(client, title="My chat!", system="sys")
    chat["messages"] = [
        _msg(
            "u",
            None,
            "user",
            "hi",
            attachments=[
                {"kind": "pdf", "name": "spec.pdf", "file": "attachments/" + "a" * 64 + ".pdf"}
            ],
        ),
        _msg("x", "u", "assistant", "old branch"),
        _msg(
            "y",
            "u",
            "assistant",
            "hello",
            reasoning="think",
            tool_calls=[
                {"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        ),
    ]
    chat["active_leaf"] = "y"
    client.put(f"/api/admin/chats/{chat['id']}", json=chat)
    md = client.get(f"/api/admin/chats/{chat['id']}/export", params={"format": "md"})
    assert md.headers["content-type"].startswith("text/markdown")
    assert 'filename="My-chat.md"' in md.headers["content-disposition"]
    text = md.text
    assert "# My chat!" in text and "## You" in text and "hello" in text
    assert "old branch" not in text and "spec.pdf" in text and "> think" in text
    js = client.get(f"/api/admin/chats/{chat['id']}/export")
    assert js.json()["id"] == chat["id"] and "attachment" in js.headers["content-disposition"]


def test_delete_all(client: TestClient, paths: Paths) -> None:
    _chat(client)
    _chat(client, title="two")
    client.post("/api/admin/chats/attachments", content=PNG, headers={"Content-Type": "image/png"})
    assert client.delete("/api/admin/chats").json() == {"deleted": 2}
    assert client.get("/api/admin/chats").json()["chats"] == []
    assert list(paths.attachments_dir.iterdir()) == []
    assert client.get("/api/admin/chats", params={"q": "two"}).json()["chats"] == []

"""The fake Hub's large-file shape (D61): HEAD → 302 to the CDN with the headers
huggingface_hub reads, and a CDN that serves byte ranges."""

from __future__ import annotations

import hashlib
import http.client
from urllib.parse import urlsplit

from hub import GGUF_35B, FakeHub, repository

WEIGHTS = "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"


def request(url: str, method: str = "GET", headers: dict[str, str] | None = None):
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port, timeout=10)
    connection.request(method, parts.path + ("?" + parts.query if parts.query else ""), headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response, body


def test_head_redirects_an_lfs_file_to_the_cdn_with_the_hub_headers():
    remote = repository(GGUF_35B)[1][WEIGHTS]
    with FakeHub() as hub:
        response, _ = request(f"{hub.url}/{GGUF_35B}/resolve/main/{WEIGHTS}", "HEAD")
        assert response.status == 302
        assert response.getheader("X-Linked-Etag") == f'"{remote.blob}"'
        assert response.getheader("X-Linked-Size") == str(remote.size)
        assert response.getheader("X-Repo-Commit") == repository(GGUF_35B)[0].commit
        location = response.getheader("Location")
        assert location.startswith(hub.cdn_url + "/xet-bridge/")
        assert urlsplit(location).netloc != urlsplit(hub.url).netloc
        assert "Signature=" in location

        whole, body = request(location)
        assert whole.status == 200
        assert hashlib.sha256(body).hexdigest() == remote.blob

        part, tail = request(location, headers={"Range": "bytes=1000-", "If-Range": '"nope"'})
        assert part.status == 206, "If-Range is ignored, as on the real xet-bridge"
        assert part.getheader("Content-Range") == f"bytes 1000-{remote.size - 1}/{remote.size}"
        assert tail == body[1000:]

        unsigned, _ = request(location.split("?")[0])
        assert unsigned.status == 403
        statuses = [(r["host"], r["method"], r["status"]) for r in hub.requests]
        assert statuses == [
            ("hub", "HEAD", None),
            ("cdn", "GET", 200),
            ("cdn", "GET", 206),
            ("cdn", "GET", 403),
        ]
        assert hub.requests[2]["headers"]["range"] == "bytes=1000-"


def test_head_of_a_small_file_answers_with_its_git_etag():
    with FakeHub() as hub:
        response, _ = request(f"{hub.url}/{GGUF_35B}/resolve/main/README.md", "HEAD")
        assert response.status == 200
        assert response.getheader("X-Linked-Etag") is None
        assert len(response.getheader("ETag").strip('"')) == 40

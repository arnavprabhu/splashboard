"""A deterministic local stand-in for the Hugging Face HTTP API.

Two clients speak to it, and both are real code paths:

  * the manager's own `HfClient` (`splash_gui/models/hf.py`), pointed at it
    with the `hf.endpoint` setting — search, `repo_info`, the model card;
  * the engine-side `install/hub.py` used by the compatibility helper
    `splash_gui/helpers/inspect_model.py`, pointed at it with `HF_ENDPOINT`.

Every fixture repository's file set comes from the fake installer's own
`models.target_repo` / `models.draft_repo`, so what the Hub advertises is
exactly what `install/models.py prepare` downloads and what `install/upstream.py`
screens. Nothing here reaches the network.

Large-file downloads (D61) take the real Hub's shape: `HEAD …/resolve/REV/FILE` for
an LFS file answers `302` with `X-Repo-Commit`, `X-Linked-Etag` (the sha256),
`X-Linked-Size`, `X-Xet-Hash` and a `Location` on a second server, the fake CDN
(`cdn_url`, another port, so a different host for the token rule). The CDN serves
`/xet-bridge/REPO/XETHASH?Expires=…&Signature=…` with `Range: bytes=N-` → `206` and
`Content-Range`, ignores `If-Range` (as the real xet-bridge does), answers `403`
without a signature, and streams at `cdn_bps` (env `FAKE_HUB_CDN_BPS`, default
unthrottled). Every request to either server is recorded in `requests`.

Usage from a test:

    with FakeHub() as hub:
        state.settings.set_global({"hf": {"endpoint": hub.url}})
        ...
    # or, for the engine-side helper:
    env["HF_ENDPOINT"] = hub.url
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).parent / "pkg"))

from install import models, signatures

# Fixture repositories. The name decides the fake architecture
# (`models.family_of`), so each one exercises a different compatibility outcome.
MLX_35B = "mlx-community/Qwen3.6-35B-A3B-4bit"
MLX_27B = "mlx-community/Qwen3.8-27B-4bit"
GGUF_35B = "unsloth/Qwen3.6-35B-A3B-GGUF"
GGUF_27B = "prism-ml/Ternary-Bonsai-2-27B-gguf"
# A GGUF repository that publishes no vision projector, so the "Compatible —
# text only" path and the language-only guard can both be exercised.
GGUF_NO_VISION = "unsloth/Qwen3.6-35B-A3B-GGUF-textonly"
MLX_8BIT = "mlx-community/Qwen3.6-35B-A3B-8bit"
LEGACY = "incoai/Qwen3.6-35B-A3B-Splash"
# The two repositories the SPEC §21 acceptance checklist searches for.
ACCEPT_8BIT = "mlx-community/Qwen3.8-27B-8bit"
ACCEPT_GGUF = "unsloth/Qwen3.8-27B-GGUF"

_ROUTE_LOCK = threading.Lock()

CARD = "# Fixture model\n\nA local stand-in, served by scripts/fake_splash/hub.py.\n"


def repository(repo_id: str, revision: str | None = None) -> tuple[models.RemoteRepo, dict, dict]:
    """(repo, files by name, config.json) for a fixture repository.

    The synthetic config carries the family's real architecture rather than the
    fake tensor bytes, because that is what the engine's model-check screens and
    what `install/upstream.py` reads before any weights exist.
    """
    family = models.family_of(repo_id)
    if not family:
        raise ValueError("unknown fixture repository")
    model = repo_id + ":UD-Q4_K_M" if "GGUF" in repo_id or repo_id.endswith("-gguf") else repo_id
    selection = models.Selection.of(
        models.paths.MODELS,
        model,
        revision=revision,
        language_only="textonly" in repo_id,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        if "DFlash2" in repo_id:
            repo = models.draft_repo(selection, family)
        else:
            repo, _, _, _ = models.target_repo(selection, family)
    if repo_id == ACCEPT_GGUF:
        repo.files += more_variants(repo)
    files = {f.name: f for f in repo.files}
    config = {
        "text_config": signatures.text_config(family),
        "quantization": {"bits": 4, "group_size": 64, "mode": "affine"},
    }
    if "8bit" in repo_id:
        # The rejection Splash reports for anything but affine 4-bit group 64.
        config["quantization"]["bits"] = 8
    return repo, files, config


# The real unsloth/Qwen3.8-27B-GGUF lists 25 root GGUFs (2026-10-07). Its fixture
# lists some of them, sized in proportion to UD-Q4_K_M (the installer's own file),
# so the §9.1 pick, the refusals (UD-Q8_K_XL, the imatrix file) and the order the
# compatibility check reports them in (D59) can be tested. Only UD-Q4_K_M downloads.
MORE_VARIANTS = {
    "UD-IQ2_XXS": 7_266_070_528,
    "UD-Q2_K_XL": 9_828_981_664,
    "Q4_0": 16_056_478_688,
    "UD-Q4_K_XL": 17_559_178_144,
    "Q8_0": 29_047_086_048,
    "UD-Q8_K_XL": 31_457_991_680,
}
Q4_K_M_BYTES = 16_464_440_224


def more_variants(repo: models.RemoteRepo) -> list[models.RemoteFile]:
    base = next(f for f in repo.files if f.name.endswith("-UD-Q4_K_M.gguf"))
    stem = base.name.removesuffix("-UD-Q4_K_M.gguf")
    files = [
        models.RemoteFile(
            f"{stem}-{name}.gguf",
            max(1, base.size * size // Q4_K_M_BYTES),
            True,
            base.header,
            base.seed + name.encode(),
        )
        for name, size in MORE_VARIANTS.items()
    ]
    files.append(models.RemoteFile("imatrix_unsloth.gguf", 4096, True, b"GGUF", b"imatrix"))
    return files


def legacy_manifest() -> dict:
    """A schema-3 `manifest.json`, the only thing the legacy branch reads."""
    return {
        "schema_version": 3,
        "model": "Qwen3.6-35B-A3B",
        "vision": True,
        "targets": [],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: object, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def do_HEAD(self) -> None:
        path = unquote(urlsplit(self.path).path)
        hub: FakeHub = self.server.hub  # type: ignore[attr-defined]
        hub.record("hub", self)
        try:
            with _ROUTE_LOCK:
                if "/resolve/" not in path:
                    raise KeyError(path)
                name, revision, filename = self._resolve(path)
                self._head(hub, name, revision, filename)
        except (ValueError, KeyError):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def _head(self, hub: FakeHub, name: str, revision: str, filename: str) -> None:
        """huggingface_hub's `get_hf_file_metadata` reads these headers. An LFS file
        redirects to the CDN, as an Xet-backed file on the real Hub does."""
        repo, files, _ = repository(name, revision)
        remote = files.get(filename)
        if remote is None or not remote.lfs:
            body, _ = self._body(name, revision, filename)
            oid = hashlib.sha1(f"blob {len(body)}\0".encode() + body, usedforsecurity=False)
            self.send_response(200)
            self.send_header("ETag", f'"{oid.hexdigest()}"')
            self.send_header("X-Repo-Commit", repo.commit)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return
        sha = remote.blob
        xet = hashlib.sha256(b"xet:" + sha.encode()).hexdigest()
        hub.cdn_files[xet] = remote
        expires = int(time.time()) + 3600
        signature = hashlib.sha256(f"{xet}{expires}".encode()).hexdigest()[:32]
        self.send_response(302)
        self.send_header(
            "Location",
            f"{hub.cdn_url}/xet-bridge/{name}/{xet}?Expires={expires}&Signature={signature}",
        )
        self.send_header("X-Repo-Commit", repo.commit)
        self.send_header("X-Linked-Etag", f'"{sha}"')
        self.send_header("X-Linked-Size", str(remote.size))
        self.send_header("X-Xet-Hash", xet)
        self.send_header("ETag", f'W/"{hashlib.sha1(sha.encode(), usedforsecurity=False).hexdigest()}"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        self.server.hub.record("hub", self)  # type: ignore[attr-defined]
        try:
            # The fake installer's selection code swaps sys.stdout and shares
            # module state, so concurrent requests (the catalog fetches every
            # repository at once) are answered one at a time.
            with _ROUTE_LOCK:
                self._route(path)
        except (ValueError, KeyError):
            self._json({"error": "Repository Not Found"}, 404)

    def _route(self, path: str) -> None:
        if path == "/api/models":
            self._json([self._search_entry(name) for name in _catalog()])
        elif path.startswith("/api/models/"):
            self._repo_info(path.removeprefix("/api/models/"))
        elif "/resolve/" in path:
            name, revision, filename = self._resolve(path)
            self._file(name, revision, filename)
        elif "/raw/" in path:
            name, revision, _ = self._resolve(path.replace("/raw/", "/resolve/"))
            self._file(name, revision, "README.md")
        else:
            self._json({"error": "Not Found"}, 404)

    @staticmethod
    def _search_entry(repo_id: str) -> dict:
        kind = "gguf" if "GGUF" in repo_id or repo_id.endswith("-gguf") else "mlx"
        return {
            "id": repo_id,
            "modelId": repo_id,
            "tags": [kind, "license:apache-2.0"],
            "downloads": 1000,
            "likes": 10,
            "lastModified": "2026-10-01T00:00:00.000Z",
        }

    def _repo_info(self, rest: str) -> None:
        repo_id, _, revision = rest.partition("/revision/")
        if revision and len(revision) != 40:
            raise ValueError("revision is not a commit")
        repo, _, _ = repository(repo_id, revision or None)
        siblings = []
        for remote in repo.files:
            siblings.append(
                {
                    "rfilename": remote.name,
                    "size": remote.size,
                    "blobId": remote.blob,
                    "lfs": {"size": remote.size, "sha256": remote.blob} if remote.lfs else None,
                }
            )
        if repo_id == LEGACY:
            siblings.append({"rfilename": "manifest.json", "size": 64, "lfs": None})
        self._json(
            {
                "id": repo_id,
                "modelId": repo_id,
                "sha": repo.commit,
                "tags": self._search_entry(repo_id)["tags"],
                "downloads": 1000,
                "likes": 10,
                "lastModified": "2026-10-01T00:00:00.000Z",
                "cardData": {"license": "apache-2.0"},
                "siblings": siblings,
            }
        )

    @staticmethod
    def _resolve(path: str) -> tuple[str, str, str]:
        name, rest = path.lstrip("/").split("/resolve/", 1)
        revision, filename = rest.split("/", 1)
        return name, revision, filename

    def _file(self, name: str, revision: str, filename: str) -> None:
        self._send(200, *self._body(name, revision, filename))

    @staticmethod
    def _body(name: str, revision: str, filename: str) -> tuple[bytes, str]:
        if name == LEGACY:
            if filename != "manifest.json":
                raise KeyError(filename)
            return json.dumps(legacy_manifest()).encode(), "application/json"
        _, files, config = repository(name, revision)
        if filename == "config.json":
            return json.dumps(config).encode(), "application/json"
        if filename == "README.md":
            return CARD.encode(), "text/markdown; charset=utf-8"
        if filename in files:
            remote = files[filename]
            return remote.read(0, remote.size), "application/octet-stream"
        raise KeyError(filename)


_RANGE = re.compile(r"^bytes=(\d+)-(\d*)$")


class CdnHandler(BaseHTTPRequestHandler):
    """The fake CDN behind an LFS redirect (the real one is `us.aws.cdn.hf.co`'s
    xet-bridge): byte ranges, no auth, a signed query string."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        hub: FakeHub = self.server.hub  # type: ignore[attr-defined]
        entry = hub.record("cdn", self)
        parts = urlsplit(self.path)
        if "Signature" not in parse_qs(parts.query):
            entry["status"] = 403
            self._empty(403)
            return
        remote = hub.cdn_files.get(parts.path.rsplit("/", 1)[-1])
        if remote is None:
            entry["status"] = 404
            self._empty(404)
            return
        start, end, status = 0, remote.size - 1, 200
        wanted = self.headers.get("Range")
        if wanted:
            match = _RANGE.match(wanted)
            if match is None or int(match.group(1)) >= remote.size:
                entry["status"] = 416
                self._empty(416)
                return
            start = int(match.group(1))
            end = min(end, int(match.group(2))) if match.group(2) else end
            status = 206
        entry["status"] = status
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        # The xet-bridge's ETag is the Xet hash, not the sha256; If-Range is ignored.
        self.send_header("ETag", f'"{parts.path.rsplit("/", 1)[-1]}"')
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{remote.size}")
        self.end_headers()
        offset, began = start, time.monotonic()
        try:
            while offset <= end:
                chunk = remote.read(offset, min(64 << 10, end + 1 - offset))
                self.wfile.write(chunk)
                offset += len(chunk)
                rate = hub.cdn_bps
                if rate:
                    ahead = (offset - start) / rate - (time.monotonic() - began)
                    if ahead > 0:
                        time.sleep(ahead)
        except (BrokenPipeError, ConnectionResetError):
            return


def _catalog() -> list[str]:
    return [
        MLX_35B,
        MLX_27B,
        GGUF_35B,
        GGUF_27B,
        GGUF_NO_VISION,
        MLX_8BIT,
        LEGACY,
        ACCEPT_8BIT,
        ACCEPT_GGUF,
    ]


class FakeHub:
    """A running fake Hub and its CDN. Use as a context manager; `.url` is the
    endpoint, `.requests` every request either server received."""

    def __init__(self) -> None:
        self.cdn_files: dict[str, models.RemoteFile] = {}
        self.cdn_bps = int(os.environ.get("FAKE_HUB_CDN_BPS") or 0)
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._servers = [
            ThreadingHTTPServer(("127.0.0.1", 0), Handler),
            ThreadingHTTPServer(("127.0.0.1", 0), CdnHandler),
        ]
        self._threads = []
        for server in self._servers:
            server.hub = self  # type: ignore[attr-defined]
            server.daemon_threads = True
            self._threads.append(threading.Thread(target=server.serve_forever, daemon=True))

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._servers[0].server_address[1]}"

    @property
    def cdn_url(self) -> str:
        return f"http://127.0.0.1:{self._servers[1].server_address[1]}"

    def record(self, host: str, handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        """Note a request; the CDN fills in `status` once it has answered."""
        parts = urlsplit(handler.path)
        entry: dict[str, Any] = {
            "host": host,
            "method": handler.command,
            "path": unquote(parts.path),
            "query": parts.query,
            "headers": {k.lower(): v for k, v in handler.headers.items()},
            "status": None,
        }
        with self._lock:
            self.requests.append(entry)
        return entry

    def requests_to(self, host: str) -> list[dict[str, Any]]:
        with self._lock:
            return [r for r in self.requests if r["host"] == host]

    def start(self) -> FakeHub:
        for thread in self._threads:
            thread.start()
        return self

    def stop(self) -> None:
        for server, thread in zip(self._servers, self._threads, strict=True):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def __enter__(self) -> FakeHub:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()


def serve() -> Iterator[str]:
    with FakeHub() as hub:
        yield hub.url


if __name__ == "__main__":
    with FakeHub() as running:
        print(running.url, flush=True)
        threading.Event().wait()

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

Usage from a test:

    with FakeHub() as hub:
        state.settings.set_global({"hf": {"endpoint": hub.url}})
        ...
    # or, for the engine-side helper:
    env["HF_ENDPOINT"] = hub.url
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

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
    files = {f.name: f for f in repo.files}
    config = {
        "text_config": signatures.text_config(family),
        "quantization": {"bits": 4, "group_size": 64, "mode": "affine"},
    }
    if "8bit" in repo_id:
        # The rejection Splash reports for anything but affine 4-bit group 64.
        config["quantization"]["bits"] = 8
    return repo, files, config


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

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
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
        if name == LEGACY:
            if filename != "manifest.json":
                raise KeyError(filename)
            self._json(legacy_manifest())
            return
        _, files, config = repository(name, revision)
        if filename == "config.json":
            self._json(config)
        elif filename == "README.md":
            self._send(200, CARD.encode(), "text/markdown; charset=utf-8")
        elif filename in files:
            remote = files[filename]
            self._send(200, remote.read(0, remote.size), "application/octet-stream")
        else:
            raise KeyError(filename)


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
    """A running fake Hub. Use as a context manager; `.url` is the endpoint."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> FakeHub:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

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

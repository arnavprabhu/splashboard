"""Hugging Face Hub HTTP calls the manager makes itself (SPEC §9.1–§9.3):
search, repository info (files, sizes, commit, license, tags), the model card.

The token is the Keychain override, else `HF_TOKEN`, else the `hf auth login`
token (D10); `hf.endpoint` replaces https://huggingface.co for mirrors. Tests set
`transport` to an `httpx.MockTransport`.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

import httpx

from ..secrets import SecretName, SecretsError

if TYPE_CHECKING:
    from ..state import ManagerState

DEFAULT_ENDPOINT = "https://huggingface.co"
TIMEOUT_S = 15.0
# SPEC §9.2: the Hub filters on these tags (`filter=` ANDs, so one request per tag).
SEARCH_TAGS = ("mlx", "gguf")
_FRONT_MATTER = re.compile(r"\A---\s*\n.*?\n---\s*(\n|\Z)", re.DOTALL)
# A Hub blob ID (a file's LFS sha256, or a git object ID for a small file) names a file in
# the Hub cache, and downloads and removal join it into a path, so only this shape is kept
# (SPEC §9.4). The downloads queue checks it again when it loads downloads.json.
BLOB_ID = re.compile(r"[0-9a-f]{40,64}")


class HubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def strip_front_matter(text: str) -> str:
    return _FRONT_MATTER.sub("", text, count=1).lstrip("\n")


def is_blob_id(value: object) -> TypeGuard[str]:
    """Whether `value` is a Hub blob ID, the only kind of value that may name a file in
    the blobs folder (`BLOB_ID`)."""
    return isinstance(value, str) and BLOB_ID.fullmatch(value) is not None


def login_token() -> str | None:
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"]
    try:
        from huggingface_hub import constants

        token = Path(constants.HF_TOKEN_PATH).read_text(encoding="utf-8").strip()
    except Exception:
        return None
    return token or None


@dataclass
class RepoInfo:
    repo_id: str
    sha: str | None
    files: dict[str, int | None]  # path -> size
    blobs: dict[str, str] = field(default_factory=dict)  # path -> blob id (lfs sha256 or git oid)
    license: str | None = None
    tags: list[str] = field(default_factory=list)
    gated: bool = False
    last_modified: str | None = None
    downloads: int | None = None
    likes: int | None = None


class HfClient:
    def __init__(self, state: ManagerState) -> None:
        self.state = state
        self.transport: httpx.AsyncBaseTransport | None = None

    @property
    def endpoint(self) -> str:
        return (self.state.settings.current.global_.hf.endpoint or DEFAULT_ENDPOINT).rstrip("/")

    def token(self) -> str | None:
        try:
            override = self.state.secrets.get(SecretName.HF_TOKEN)
        except SecretsError:
            override = None
        return override or login_token()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "splash-gui"}
        token = self.token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        if self.state.settings.current.global_.hf.offline:
            raise HubError(503, "Hugging Face is off (offline mode is on)")
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT_S, transport=self.transport, follow_redirects=True
            ) as client:
                response = await client.get(
                    self.endpoint + path, params=params, headers=self._headers()
                )
        except httpx.HTTPError as error:
            raise HubError(503, f"Hugging Face is unreachable: {error}") from None
        if response.status_code in (401, 403):
            raise HubError(
                response.status_code,
                "Gated or private: add a Hugging Face token in Settings → Hugging Face",
            )
        if response.status_code == 404:
            raise HubError(404, "No such repository on Hugging Face")
        if response.status_code >= 400:
            raise HubError(502, f"Hugging Face answered {response.status_code}")
        return response

    async def search(self, query: str, sort: str, limit: int) -> list[dict[str, Any]]:
        """SPEC §9.2: MLX and GGUF repositories only. The Hub ANDs repeated `filter`
        values, so each format is one request; the two sorted lists are merged by the
        same key, de-duplicated and cut to `limit`."""
        sort_key = {"downloads": "downloads", "likes": "likes", "recent": "lastModified"}[sort]
        batches = await asyncio.gather(
            *(
                self._get(
                    "/api/models",
                    {
                        "search": query,
                        "filter": tag,
                        "sort": sort_key,
                        "direction": "-1",
                        "limit": str(limit),
                        "full": "true",
                    },
                )
                for tag in SEARCH_TAGS
            )
        )
        rows: dict[str, dict[str, Any]] = {}
        for response in batches:
            data = response.json()
            for row in data if isinstance(data, list) else []:
                if isinstance(row, dict):
                    rows.setdefault(str(row.get("id") or row.get("modelId") or ""), row)

        def order(row: dict[str, Any]) -> Any:
            # Missing values sort last: ISO dates compare as text, counts as numbers.
            value = row.get(sort_key)
            if sort == "recent":
                return value if isinstance(value, str) else ""
            return value if isinstance(value, int | float) and not isinstance(value, bool) else -1

        return sorted(rows.values(), key=order, reverse=True)[:limit]

    async def repo_info(self, repo_id: str, revision: str | None = None) -> RepoInfo:
        path = f"/api/models/{repo_id}" + (f"/revision/{revision}" if revision else "")
        response = await self._get(path, {"blobs": "true"})
        data = response.json()
        files: dict[str, int | None] = {}
        blobs: dict[str, str] = {}
        for sibling in data.get("siblings") or []:
            if not isinstance(sibling, dict) or not isinstance(sibling.get("rfilename"), str):
                continue
            name = sibling["rfilename"]
            size = sibling.get("size")
            lfs = sibling.get("lfs") if isinstance(sibling.get("lfs"), dict) else None
            if lfs and isinstance(lfs.get("size"), int):
                size = lfs["size"]
            files[name] = size if isinstance(size, int) else None
            blob = (lfs or {}).get("sha256") or sibling.get("blobId")
            # The blob ID names a file in the Hub cache (downloads and removal join it to the
            # blobs folder), so only a hash is kept; anything else is ignored, not joined.
            if is_blob_id(blob):
                blobs[name] = blob
        card = data.get("cardData") if isinstance(data.get("cardData"), dict) else {}
        license_ = card.get("license") if isinstance(card.get("license"), str) else None
        tags = [t for t in data.get("tags") or [] if isinstance(t, str)]
        if license_ is None:
            license_ = next((t.split(":", 1)[1] for t in tags if t.startswith("license:")), None)
        return RepoInfo(
            repo_id=str(data.get("id") or data.get("modelId") or repo_id),
            sha=data.get("sha") if isinstance(data.get("sha"), str) else None,
            files=files,
            blobs=blobs,
            license=license_,
            tags=tags,
            gated=bool(data.get("gated")),
            last_modified=data.get("lastModified"),
            downloads=data.get("downloads") if isinstance(data.get("downloads"), int) else None,
            likes=data.get("likes") if isinstance(data.get("likes"), int) else None,
        )

    async def readme(self, repo_id: str, revision: str | None = None) -> str:
        try:
            response = await self._get(f"/{repo_id}/raw/{revision or 'main'}/README.md")
        except HubError as error:
            if error.status == 404:
                return ""
            raise
        return response.text

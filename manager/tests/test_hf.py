"""`models/hf.py`: the manager's own Hugging Face calls (SPEC §9.1–§9.3, D10)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from splash_gui.models.hf import HfClient, HubError, login_token, strip_front_matter
from splash_gui.secrets import SecretName


def client_for(app: FastAPI, handler: Any) -> tuple[HfClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response: httpx.Response = handler(request)
        return response

    hf = HfClient(app.state.manager)
    hf.transport = httpx.MockTransport(record)
    return hf, seen


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_strip_front_matter() -> None:
    assert strip_front_matter("---\nlicense: mit\n---\n\n# Title\n") == "# Title\n"
    assert strip_front_matter("# No front matter") == "# No front matter"
    assert strip_front_matter("---\nonly: header\n---") == ""


def test_search_maps_sort_and_sends_the_query(app: FastAPI) -> None:
    hf, seen = client_for(app, lambda r: httpx.Response(200, json=[{"id": "a/b"}, "junk"]))
    result = run(hf.search("qwen3.8", "recent", 50))
    assert result == [{"id": "a/b"}]
    params = seen[0].url.params
    assert seen[0].url.path == "/api/models"
    assert params["search"] == "qwen3.8" and params["sort"] == "lastModified"
    assert params["limit"] == "50" and params["direction"] == "-1"
    assert seen[0].headers["user-agent"] == "splash-gui"
    assert "authorization" not in seen[0].headers


def test_repo_info_reads_sizes_blobs_license_and_tags(app: FastAPI) -> None:
    body = {
        "id": "unsloth/Qwen3.8-27B-GGUF",
        "sha": "c0ffee",
        "siblings": [
            {
                "rfilename": "Qwen3.8-27B-UD-Q4_K_M.gguf",
                "size": 1,
                "lfs": {"size": 17_000_000_000, "sha256": "abc"},
            },
            {"rfilename": "README.md", "size": 1200, "blobId": "git1"},
            {"rfilename": 5},
        ],
        "tags": ["gguf", "license:apache-2.0"],
        "gated": False,
        "lastModified": "2026-09-30T00:00:00.000Z",
        "downloads": 42,
        "likes": 7,
    }
    hf, seen = client_for(app, lambda r: httpx.Response(200, json=body))
    info = run(hf.repo_info("unsloth/Qwen3.8-27B-GGUF", revision="main"))
    assert seen[0].url.path == "/api/models/unsloth/Qwen3.8-27B-GGUF/revision/main"
    assert info.sha == "c0ffee"
    assert info.files == {"Qwen3.8-27B-UD-Q4_K_M.gguf": 17_000_000_000, "README.md": 1200}
    assert info.blobs == {"Qwen3.8-27B-UD-Q4_K_M.gguf": "abc", "README.md": "git1"}
    assert info.license == "apache-2.0", "falls back to the license: tag"
    assert info.downloads == 42 and info.likes == 7


def test_card_license_wins_over_the_tag(app: FastAPI) -> None:
    body = {"id": "a/b", "cardData": {"license": "mit"}, "tags": ["license:other"]}
    hf, _ = client_for(app, lambda r: httpx.Response(200, json=body))
    assert run(hf.repo_info("a/b")).license == "mit"


@pytest.mark.parametrize(
    ("status", "expected", "text"),
    [(401, 401, "Gated or private"), (403, 403, "token"), (404, 404, "No such"), (500, 502, "500")],
)
def test_errors_are_plain_language(app: FastAPI, status: int, expected: int, text: str) -> None:
    hf, _ = client_for(app, lambda r: httpx.Response(status))
    with pytest.raises(HubError) as error:
        run(hf.repo_info("a/b"))
    assert error.value.status == expected and text in error.value.message


def test_unreachable_hub(app: FastAPI) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    hf, _ = client_for(app, fail)
    with pytest.raises(HubError) as error:
        run(hf.search("x", "downloads", 5))
    assert error.value.status == 503 and "unreachable" in error.value.message


def test_offline_mode_makes_no_request(app: FastAPI) -> None:
    store = app.state.manager.settings
    document = store.current.model_dump(mode="json", by_alias=True)
    document["global"]["hf"]["offline"] = True
    assert store.save(document)[0].ok
    hf, seen = client_for(app, lambda r: httpx.Response(200, json=[]))
    with pytest.raises(HubError) as error:
        run(hf.search("x", "downloads", 5))
    assert error.value.status == 503 and seen == []


def test_endpoint_override_for_mirrors(app: FastAPI) -> None:
    store = app.state.manager.settings
    document = store.current.model_dump(mode="json", by_alias=True)
    document["global"]["hf"]["endpoint"] = "https://hf-mirror.example/"
    assert store.save(document)[0].ok
    hf, seen = client_for(app, lambda r: httpx.Response(200, text="# Card"))
    assert run(hf.readme("a/b")) == "# Card"
    assert str(seen[0].url) == "https://hf-mirror.example/a/b/raw/main/README.md"


def test_readme_missing_is_empty(app: FastAPI) -> None:
    hf, _ = client_for(app, lambda r: httpx.Response(404))
    assert run(hf.readme("a/b", "c0ffee")) == ""


def test_token_order_keychain_then_env_then_login(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    hf, seen = client_for(app, lambda r: httpx.Response(200, json=[]))
    token_file = isolated_home / "hf-home" / "token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("hf_login\n")
    from huggingface_hub import constants

    monkeypatch.setattr(constants, "HF_TOKEN_PATH", str(token_file))
    assert login_token() == "hf_login"
    run(hf.search("x", "downloads", 1))
    assert seen[-1].headers["authorization"] == "Bearer hf_login"
    monkeypatch.setenv("HF_TOKEN", "hf_env")
    run(hf.search("x", "downloads", 1))
    assert seen[-1].headers["authorization"] == "Bearer hf_env"
    app.state.manager.secrets.set(SecretName.HF_TOKEN, "hf_keychain")
    run(hf.search("x", "downloads", 1))
    assert seen[-1].headers["authorization"] == "Bearer hf_keychain"

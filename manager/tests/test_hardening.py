"""Security headers, the admin page's CSP and the request body cap."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from splash_gui.hardening import Hardening, spa_csp


def small_app(limit: int) -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"n": len(await request.body())}

    @app.get("/page")
    def page() -> dict[str, str]:
        return {"ok": "yes"}

    app.add_middleware(Hardening, max_body=limit)
    return app


def test_every_response_refuses_framing_and_sniffing() -> None:
    response = TestClient(small_app(100)).get("/page")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_bodies_over_the_cap_get_413() -> None:
    client = TestClient(small_app(100))
    assert client.post("/echo", content=b"x" * 100).json() == {"n": 100}
    declared = client.post("/echo", content=b"x" * 101)
    assert declared.status_code == 413
    assert declared.json()["error"]["code"] == "body_too_large"

    def chunks() -> Iterator[bytes]:  # no Content-Length: the streamed count is capped
        for _ in range(5):
            yield b"x" * 50

    streamed = client.post("/echo", content=chunks())
    assert streamed.status_code == 413


def test_spa_csp_allows_the_inline_theme_script_by_hash(tmp_path: Path) -> None:
    index = tmp_path / "index.html"
    index.write_text("<script>var a=1</script><script type=module src=/x.js></script>")
    csp = spa_csp(index)
    assert "script-src 'self' 'sha256-" in csp
    assert "unsafe-eval" not in csp and "frame-ancestors 'none'" in csp
    assert csp.count("sha256-") == 1


@pytest.mark.parametrize("path", ["/api/admin/docs"])
def test_swagger_ui_is_not_served(path: str, client: TestClient) -> None:
    assert client.get(path).status_code == 404

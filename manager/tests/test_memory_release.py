"""Release memory now: the admin action, the public /v1/memory/release and the
`splash release-memory` command, through the fake engine."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from splash_gui import cli as cli_module

from .fakeengine import MODEL, EngineHarness

H = Callable[..., EngineHarness]
CHAT = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
BUSY = "The engine is busy; try again when requests finish."


@pytest.fixture
def h_ready(harness_factory: H) -> EngineHarness:
    h = harness_factory(env={"FAKE_SPLASH_RESTORE_SECONDS": "1.5"})
    view = h.load()
    assert view["state"] == "ready", view
    return h


def release(h: EngineHarness) -> Any:
    return h.client.post("/api/admin/engine/release-memory")


def weights(h: EngineHarness) -> dict[str, Any]:
    status = h.client.get("/api/admin/engine/status")
    assert status.status_code == 200, status.text
    return dict(status.json()["weights"])


def release_calls(h: EngineHarness) -> list[dict[str, Any]]:
    return [r for r in h.last_engine_requests() if r["path"] == "/v1/memory/release"]


def wait_restoring(h: EngineHarness) -> None:
    deadline = time.monotonic() + 5
    while not weights(h)["restoring"]:
        assert time.monotonic() < deadline, "the weights never started restoring"
        time.sleep(0.05)


def test_release_frees_the_weights_and_shows_at_once(h_ready: EngineHarness) -> None:
    h = h_ready
    response = release(h)
    assert response.status_code == 200, response.text
    assert response.json() == {"released": True}
    # The status is read again after the release: no wait for the next poll.
    assert h.engine()["state"] == "idle_released"
    assert weights(h)["released"] is True
    calls = release_calls(h)
    assert calls and calls[-1]["body"] is None, "the route takes no fields"
    # Already released is still a release.
    assert release(h).json() == {"released": True}
    # The next request restores them.
    assert h.client.post("/v1/chat/completions", json=CHAT).status_code == 200
    h.wait_state("ready")


def test_release_while_the_engine_restores_is_busy(h_ready: EngineHarness) -> None:
    h = h_ready
    assert release(h).status_code == 200
    worker = threading.Thread(
        target=h.client.post, args=("/v1/chat/completions",), kwargs={"json": CHAT}
    )
    worker.start()
    try:
        wait_restoring(h)
        busy = release(h)
        assert busy.status_code == 409
        assert busy.json()["error"]["message"] == BUSY
        assert busy.json()["error"]["code"] == "engine_busy"
    finally:
        worker.join(20)


def test_release_without_a_model_is_refused(harness_factory: H) -> None:
    h = harness_factory()
    response = release(h)
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "No model is loaded."
    assert response.json()["error"]["code"] == "engine_not_loaded"


def test_an_engine_that_does_not_answer_in_time_is_busy_too(
    h_ready: EngineHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Splash answers 503 engine_busy when its engine doesn't answer within 5 s."""

    async def slow(content: bytes | None = None) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "error": {
                    "message": "the engine did not answer within 5 s; retry shortly",
                    "type": "server_error",
                    "code": "engine_busy",
                }
            },
        )

    monkeypatch.setattr(h_ready.state.proxy, "release_engine_memory", slow)
    response = release(h_ready)
    assert (response.status_code, response.json()["error"]["message"]) == (409, BUSY)


def test_other_engine_refusals_keep_their_message(
    h_ready: EngineHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def shutting_down(content: bytes | None = None) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "error": {
                    "message": "server is shutting down",
                    "type": "server_error",
                    "code": "server_shutdown",
                }
            },
        )

    monkeypatch.setattr(h_ready.state.proxy, "release_engine_memory", shutting_down)
    response = release(h_ready)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "server_shutdown"
    assert response.json()["error"]["message"] == "server is shutting down"


def test_an_engine_client_error_answers_502_and_is_documented(
    h_ready: EngineHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refused(content: bytes | None = None) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "invalid api key", "code": "invalid_api_key"}},
        )

    monkeypatch.setattr(h_ready.state.proxy, "release_engine_memory", refused)
    response = release(h_ready)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert response.json()["error"]["message"] == "invalid api key"
    route = h_ready.client.get("/api/admin/openapi.json")
    assert route.status_code == 200, route.text
    documented = route.json()["paths"]["/api/admin/engine/release-memory"]["post"]["responses"]
    assert {"200", "409", "502", "503"} <= set(documented)


def test_public_route_forwards_and_needs_the_api_key(h_ready: EngineHarness) -> None:
    h = h_ready
    secret = h.client.post("/api/admin/settings/secrets/api-key").json()["key"]
    h.patch_settings({"global": {"security": {"api_key_required": True}}})
    cli_token = h.client.headers.pop("Authorization")
    try:
        assert h.client.post("/v1/memory/release").status_code == 401
        auth = {"Authorization": f"Bearer {secret}"}
        response = h.client.post("/v1/memory/release", headers=auth)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["released"] is True and body["weights"]["released"] is True
        assert h.sup.state == "idle_released", "not counted as a request in flight"
        # The engine judges the body: it takes no fields.
        fields = h.client.post("/v1/memory/release", headers=auth, json={"now": True})
        assert fields.status_code == 400
        assert fields.json()["error"]["message"] == "this endpoint takes no fields"
        assert h.client.post("/v1/memory/release", headers=auth, json={}).status_code == 200
    finally:
        h.client.headers["Authorization"] = cli_token


def test_public_route_without_an_engine_is_unavailable(harness_factory: H) -> None:
    h = harness_factory()
    response = h.client.post("/v1/memory/release")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "engine_unavailable"


def test_public_route_answers_a_preflight(h_ready: EngineHarness) -> None:
    h_ready.patch_settings({"global": {"server": {"allowed_origins": ["tauri://localhost"]}}})
    response = h_ready.client.options(
        "/v1/memory/release",
        headers={"Origin": "tauri://localhost", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 204
    assert "POST" in response.headers["access-control-allow-methods"]


# --- splash release-memory --------------------------------------------------------


class _Shared:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def close(self) -> None:
        return None


@pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")
def test_cli_release_memory(
    harness_factory: H, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h = harness_factory()
    real = cli_module.Client

    class Client(real):  # type: ignore[misc, valid-type]
        def __init__(self, port: int | None = None) -> None:
            super().__init__(port)
            self.http.close()
            self.http: Any = _Shared(h.client)

    monkeypatch.setattr(cli_module, "Client", Client)
    monkeypatch.delenv("SPLASH_PORT", raising=False)
    code = cli_module.main(["release-memory"])
    _, err = capsys.readouterr()
    assert code == 4 and "No model is loaded." in err
    h.load()
    code = cli_module.main(["release-memory"])
    out, _ = capsys.readouterr()
    assert code == 0 and "Memory released" in out
    assert h.engine()["state"] == "idle_released"


@pytest.mark.parametrize(("status", "code"), [(500, "engine_failed"), (503, "engine_recovering")])
def test_a_failed_or_recovering_engine_answers_503(
    h_ready: EngineHarness, monkeypatch: pytest.MonkeyPatch, status: int, code: str
) -> None:
    async def answer(content: bytes | None = None) -> httpx.Response:
        return httpx.Response(
            status, json={"error": {"message": "engine is " + code, "code": code}}
        )

    monkeypatch.setattr(h_ready.state.proxy, "release_engine_memory", answer)
    response = release(h_ready)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == code

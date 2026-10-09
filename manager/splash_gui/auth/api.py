"""Admin auth routes (docs/api.md §12.5, D58)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from ..errors import ApiError, error_responses
from ..schemas import AuthLink, AuthState, ExchangeRequest, LoginRequest
from ..state import ManagerState, get_state
from .core import LINK_TTL_S, SESSION_COOKIE, SESSION_TTL_S
from .guard import is_same_origin

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


def _state_for(state: ManagerState, method: str | None) -> AuthState:
    required = state.settings.current.global_.security.admin_requires_key
    if method in ("session", "cli_token"):
        return AuthState(admin_requires_key=required, authenticated=True, method=method)  # type: ignore[arg-type]
    if not required:
        # Reads only: writes and secret reads still need a session (D58).
        return AuthState(admin_requires_key=False, authenticated=False, method="open")
    return AuthState(admin_requires_key=True, authenticated=False, method=None)


def _method(request: Request) -> str | None:
    return getattr(request.state, "auth_method", None)


def _client(request: Request) -> str:
    return request.client.host if request.client else ""


def _throttle(state: ManagerState, client: str) -> None:
    if not state.auth.login_allowed(client):
        raise ApiError(
            429,
            "Too many failed sign-ins; wait a minute and try again",
            "too_many_attempts",
            headers={"Retry-After": "60"},
        )


def _start_session(state: ManagerState, response: Response) -> AuthState:
    try:
        cookie = state.auth.issue_session()
    except ValueError:
        raise ApiError(
            503, "There is no API key to sign in with; restart Splashboard", "api_key_missing"
        ) from None
    response.set_cookie(
        SESSION_COOKIE,
        cookie,
        max_age=SESSION_TTL_S,
        path="/",
        httponly=True,
        samesite="strict",
    )
    return _state_for(state, "session")


@router.get("/auth/state", response_model=AuthState)
def auth_state(request: Request, state: State) -> AuthState:
    return _state_for(state, _method(request))


@router.post("/auth/login", response_model=AuthState, responses=error_responses(401, 429, 503))
def login(body: LoginRequest, request: Request, response: Response, state: State) -> AuthState:
    client = _client(request)
    _throttle(state, client)
    if not state.auth.check_api_key(body.key):
        state.auth.record_login_failure(client)
        raise ApiError(401, "That API key is not correct", "invalid_key")
    return _start_session(state, response)


@router.post("/auth/logout", status_code=204)
def logout(request: Request, state: State) -> Response:
    state.auth.revoke_session(request.cookies.get(SESSION_COOKIE))
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response


@router.post("/auth/link", response_model=AuthLink, responses=error_responses(401, 403))
def link(request: Request, state: State) -> AuthLink:
    """A one-time login link for the menu bar app and `splash open` (CLI token only)."""
    if _method(request) != "cli_token":
        raise ApiError(403, "Only the CLI token can create login links", "cli_token_required")
    return AuthLink(url=f"/admin/login?code={state.auth.issue_code()}", expires_in=LINK_TTL_S)


@router.post(
    "/auth/exchange", response_model=AuthState, responses=error_responses(401, 403, 429, 503)
)
def exchange(
    body: ExchangeRequest, request: Request, response: Response, state: State
) -> AuthState:
    """Turn a one-time code into a session. Only a same-origin browser page may do
    it (the CLI token does not stand in for that), so a code is only ever spent by
    the browser it was opened in."""
    headers = request.headers
    if not (
        is_same_origin(headers.get("origin"), headers.get("host"))
        and headers.get("sec-fetch-site") == "same-origin"
    ):
        raise ApiError(
            403, "Codes are exchanged only by the admin page in a browser", "csrf_refused"
        )
    client = _client(request)
    _throttle(state, client)
    if not state.auth.redeem_code(body.code):
        state.auth.record_login_failure(client)
        raise ApiError(
            401, "This sign-in link is invalid, used or expired; open a new one", "invalid_code"
        )
    return _start_session(state, response)

"""Admin auth routes (SPEC §14 Auth, §8.2 security.admin_requires_key, §17.1)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from ..errors import ApiError, error_responses
from ..schemas import AuthState, LoginRequest
from ..state import ManagerState, get_state
from .core import SESSION_COOKIE, SESSION_TTL_S

router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]


def _state_for(state: ManagerState, method: str | None) -> AuthState:
    required = state.settings.current.global_.security.admin_requires_key
    if method in ("session", "cli_token"):
        return AuthState(admin_requires_key=required, authenticated=True, method=method)  # type: ignore[arg-type]
    if not required:
        return AuthState(admin_requires_key=False, authenticated=True, method="open")
    return AuthState(admin_requires_key=True, authenticated=False, method=None)


def _method(request: Request) -> str | None:
    return getattr(request.state, "auth_method", None)


@router.get("/auth/state", response_model=AuthState)
def auth_state(request: Request, state: State) -> AuthState:
    return _state_for(state, _method(request))


@router.post("/auth/login", response_model=AuthState, responses=error_responses(401, 429))
def login(body: LoginRequest, response: Response, state: State) -> AuthState:
    auth = state.auth
    if not auth.login_allowed():
        raise ApiError(
            429,
            "Too many failed logins; wait a minute and try again",
            "too_many_attempts",
            headers={"Retry-After": "60"},
        )
    if not auth.check_api_key(body.key):
        auth.record_login_failure()
        raise ApiError(401, "That API key is not correct", "invalid_key")
    response.set_cookie(
        SESSION_COOKIE,
        auth.issue_session(),
        max_age=SESSION_TTL_S,
        path="/",
        httponly=True,
        samesite="strict",
    )
    return _state_for(state, "session")


@router.post("/auth/logout", status_code=204)
def logout(request: Request, state: State) -> Response:
    state.auth.revoke_session(request.cookies.get(SESSION_COOKIE))
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response

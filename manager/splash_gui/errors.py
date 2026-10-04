"""The admin API's error shape, `{"error": {"message", "type", "code"}}` (Splash's shape)."""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .schemas import ErrorResponse

_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    409: "conflict_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
    501: "not_implemented",
    503: "overloaded_error",
    507: "insufficient_storage",
}


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        code: str,
        *,
        type: str | None = None,
        issues: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.type = type or _TYPES.get(
            status, "server_error" if status >= 500 else "invalid_request_error"
        )
        self.issues = issues
        self.details = details
        self.headers = headers

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"message": self.message, "type": self.type, "code": self.code}
        if self.issues is not None:
            error["issues"] = self.issues
        if self.details is not None:
            error["details"] = self.details
        return {"error": error}

    def response(self) -> JSONResponse:
        return JSONResponse(self.body(), status_code=self.status, headers=self.headers)


def not_implemented(feature: str) -> NoReturn:
    """Routes whose implementation belongs to another build track (contract only)."""
    raise ApiError(501, f"{feature} is not implemented yet", "not_implemented")


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {status: {"model": ErrorResponse} for status in statuses}


STUB_RESPONSES = error_responses(501)
SSE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/event-stream": {}}, "description": "Server-sent events"}
}


def install_error_handlers(app: FastAPI) -> None:
    async def api_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApiError)
        return exc.response()

    async def validation_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        issues = [
            {
                "path": [p for p in item.get("loc", ()) if p != "body"],
                "key": ".".join(str(p) for p in item.get("loc", ()) if p != "body"),
                "model": None,
                "message": str(item.get("msg", "invalid")).removeprefix("Value error, "),
                "severity": "error",
                "code": str(item.get("type", "invalid")),
            }
            for item in exc.errors()
        ]
        return ApiError(422, "Request is invalid", "invalid_request", issues=issues).response()

    async def http_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return ApiError(
            exc.status_code, str(exc.detail), code, headers=getattr(exc, "headers", None)
        ).response()

    app.add_exception_handler(ApiError, api_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(StarletteHTTPException, http_error)

"""Application exceptions rendered as RFC 9457 problem details."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.context import current_request
from app.logging import get_logger

log = get_logger(__name__)

PROBLEM_JSON = "application/problem+json"


class AppError(Exception):
    status = 400
    code = "bad_request"
    title = "Bad request"

    def __init__(self, detail: str | None = None, *, code: str | None = None, extra: dict[str, Any] | None = None):
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        if code:
            self.code = code
        self.extra = extra or {}


class ValidationFailed(AppError):
    status, code, title = 422, "validation_error", "Validation failed"


class Unauthenticated(AppError):
    status, code, title = 401, "unauthenticated", "Authentication required"


class Forbidden(AppError):
    status, code, title = 403, "forbidden", "Permission denied"


class NotFound(AppError):
    status, code, title = 404, "not_found", "Not found"


class Conflict(AppError):
    status, code, title = 409, "conflict", "Conflict"


class PreconditionFailed(AppError):
    status, code, title = 412, "version_mismatch", "Resource was modified"


class TooManyRequests(AppError):
    status, code, title = 429, "rate_limited", "Too many requests"

    def __init__(self, retry_after: float, detail: str | None = None):
        super().__init__(detail)
        self.retry_after = max(1, int(retry_after + 0.999))


class UpstreamError(AppError):
    status, code, title = 502, "upstream_error", "Remote system error"


class ServiceUnavailable(AppError):
    status, code, title = 503, "unavailable", "Service unavailable"


def problem(status: int, code: str, title: str, detail: str | None = None, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"type": f"about:blank#{code}", "title": title, "status": status, "code": code}
    if detail:
        body["detail"] = detail
    rid = current_request().request_id
    if rid:
        body["request_id"] = rid
    body.update({k: v for k, v in extra.items() if v is not None})
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        resp = problem(exc.status, exc.code, exc.title, exc.detail, **exc.extra)
        if isinstance(exc, TooManyRequests):
            resp.headers["Retry-After"] = str(exc.retry_after)
        return resp

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()
        ]
        return problem(422, "validation_error", "Validation failed", "Request validation failed", errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed", 401: "unauthenticated", 403: "forbidden"}
        return problem(exc.status_code, codes.get(exc.status_code, "http_error"), str(exc.detail), None)

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, exc: IntegrityError) -> JSONResponse:
        log.info("integrity_error", error=str(exc.orig)[:300])
        return problem(409, "conflict", "Conflict", "The change conflicts with existing data")

    @app.exception_handler(OperationalError)
    async def _db_down(_: Request, exc: OperationalError) -> JSONResponse:
        log.error("database_unavailable", error=str(exc.orig)[:300])
        return problem(503, "database_unavailable", "Database unavailable", "Please retry shortly")

    @app.exception_handler(DBAPIError)
    async def _dbapi(_: Request, exc: DBAPIError) -> JSONResponse:
        log.error("database_error", error=str(exc.orig)[:300])
        return problem(503, "database_error", "Database error", "Please retry shortly")

    @app.exception_handler(OSError)
    async def _os(_: Request, exc: OSError) -> JSONResponse:
        # Typically "connection refused" while the database is down.
        log.error("os_error", error=str(exc)[:300])
        return problem(503, "unavailable", "Service unavailable", "A dependency is unreachable")

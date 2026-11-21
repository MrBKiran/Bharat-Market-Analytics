"""One error format for the whole API, and request logging.

Every error, whatever its cause, has the same JSON body:

    {"error": {"code": "not_found", "message": "No instrument called XYZ", "details": null, "request_id": "..."}}

so a frontend needs exactly one error handler. The `request_id` is also in the `X-Request-ID` response header and in the
server log line for that request: quote it when reporting a problem. Internal details (stack traces, the database address)
are logged, never sent to the client.
"""
import logging
import re
import time
import uuid
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.serialize import SafeJSONResponse

logger = logging.getLogger("app.api")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class ApiError(Exception):
    """Raise this from a route to answer with a clean error. `code` is a short machine-readable word."""

    def __init__(self, status_code: int, code: str, message: str, details: Any = None, headers: Optional[dict] = None):
        super().__init__(message)
        self.status_code, self.code, self.message, self.details, self.headers = status_code, code, message, details, headers


def error_response(request: Request, status_code: int, code: str, message: str, details: Any = None,
                   headers: Optional[dict] = None) -> SafeJSONResponse:
    body = {"error": {"code": code, "message": message, "details": details,
                      "request_id": getattr(request.state, "request_id", None)}}
    return SafeJSONResponse(body, status_code=status_code, headers=headers)


_STATUS_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
                 409: "conflict", 413: "payload_too_large", 422: "validation_error", 429: "rate_limited", 503: "unavailable"}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return error_response(request, exc.status_code, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        return error_response(request, exc.status_code, code, str(exc.detail), headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        problems = [{"where": ".".join(str(p) for p in e.get("loc", ())), "problem": e.get("msg", "invalid value")}
                    for e in exc.errors()]
        first = problems[0] if problems else {"where": "request", "problem": "invalid"}
        return error_response(request, 422, "validation_error", f"Invalid {first['where']}: {first['problem']}", problems)

    @app.exception_handler(ServerSelectionTimeoutError)
    async def _db_down(request: Request, exc: ServerSelectionTimeoutError):
        logger.error("database unavailable (%s) request_id=%s", type(exc).__name__, getattr(request.state, "request_id", None))
        return error_response(request, 503, "database_unavailable", "The database is not reachable right now. Try again shortly.")

    @app.exception_handler(PyMongoError)
    async def _db_error(request: Request, exc: PyMongoError):
        logger.error("database error %s request_id=%s", type(exc).__name__, getattr(request.state, "request_id", None), exc_info=True)
        return error_response(request, 500, "database_error", "The database rejected the request.")

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        logger.error("unhandled %s on %s %s request_id=%s", type(exc).__name__, request.method, request.url.path,
                     getattr(request.state, "request_id", None), exc_info=True)
        return error_response(request, 500, "internal_error", "Something went wrong on the server. Quote the request_id when reporting it.")


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Request id, one access-log line per request, and a few safe response headers."""

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        request.state.request_id = incoming if REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex[:16]
        started = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Response-Time-ms"] = f"{ms:.1f}"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("Cache-Control", "no-store")
        client = request.client.host if request.client else "-"
        logger.info("%s %s -> %s in %.1f ms client=%s id=%s", request.method, request.url.path, response.status_code, ms,
                    client, request.state.request_id)
        return response

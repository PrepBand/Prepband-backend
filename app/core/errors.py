import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

# Spec section 8.1: every error response carries a machine-readable code, a
# safe user-facing message, a correlation id, and a retryable flag.
_RETRYABLE_STATUS_CODES = {429, 503}

_STATUS_CODES_TO_DEFAULT_CODE = {
    400: "invalid_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}

logger = logging.getLogger(__name__)


def error_body(status_code: int, message: str, code: str | None = None) -> dict:
    return {
        "error": {
            "code": code or _STATUS_CODES_TO_DEFAULT_CODE.get(status_code, "error"),
            "message": message,
            "correlation_id": str(uuid.uuid4()),
            "retryable": status_code in _RETRYABLE_STATUS_CODES,
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # A dict detail ({"message": ..., "problems": [...]}) carries a list
        # of specific problems alongside the message - used by publish
        # validation so the admin UI can show every blocker at once.
        if isinstance(exc.detail, dict):
            body = error_body(exc.status_code, str(exc.detail.get("message", "Request failed.")))
            if "problems" in exc.detail:
                body["error"]["details"] = [{"msg": str(p)} for p in exc.detail["problems"]]
            return JSONResponse(status_code=exc.status_code, content=body)
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return JSONResponse(status_code=exc.status_code, content=error_body(exc.status_code, detail))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        body = error_body(422, "The request failed validation.", code="validation_error")
        # Only loc/msg/type: pydantic's raw errors() can carry the original
        # exception object in `ctx`, which isn't JSON-serializable and would
        # turn a 422 into a 500.
        body["error"]["details"] = [
            {"loc": list(err.get("loc", ())), "msg": str(err.get("msg", "")), "type": str(err.get("type", ""))}
            for err in exc.errors()
        ]
        return JSONResponse(status_code=422, content=body)

    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        # Two requests racing to create the same row (a unique constraint)
        # is a conflict the client can retry, not a server fault.
        body = error_body(409, "That change clashed with another one. Please try again.")
        body["error"]["retryable"] = True
        logger.warning("Integrity conflict %s on %s %s: %s", body["error"]["correlation_id"], request.method, request.url.path, exc.orig)
        return JSONResponse(status_code=409, content=body)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Never leak a stack trace or SQL to the browser (spec section 9);
        # the correlation id ties the user's report to this log line.
        body = error_body(500, "Something went wrong on our side. Please try again.")
        logger.error(
            "Unhandled error %s on %s %s", body["error"]["correlation_id"], request.method, request.url.path, exc_info=exc
        )
        return JSONResponse(status_code=500, content=body)

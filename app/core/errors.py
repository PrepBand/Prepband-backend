import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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
    422: "validation_error",
    429: "rate_limited",
    503: "service_unavailable",
}


def _error_body(status_code: int, message: str, code: str | None = None) -> dict:
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
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return JSONResponse(status_code=exc.status_code, content=_error_body(exc.status_code, detail))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        body = _error_body(422, "The request failed validation.", code="validation_error")
        body["error"]["details"] = exc.errors()
        return JSONResponse(status_code=422, content=body)

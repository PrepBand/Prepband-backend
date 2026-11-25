from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.v1.router import router as v1_router
from .core.config import get_settings
from .core.errors import error_body, register_exception_handlers
from .services.audit import install_audit_middleware

settings = get_settings()

# Interactive docs only outside production; the OpenAPI schema is still
# exported to docs/openapi.json by scripts/export_openapi.py.
_docs = settings.environment != "production"
app = FastAPI(
    title="prepband-backend",
    version="1.0.0",
    docs_url="/docs" if _docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _docs else None,
)

# Spec section 9: bounded input sizes. The largest legitimate body is a
# whole-test JSON import or a question CSV (~1 MB); essays are far smaller.
MAX_BODY_BYTES = 2_000_000

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


# Order matters: the audit middleware is added first so it sits *inside*
# the security middleware below and sees the final status of every request.
install_audit_middleware(app)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    length = request.headers.get("content-length")
    if length is not None:
        try:
            too_big = int(length) > MAX_BODY_BYTES
        except ValueError:
            too_big = True
        if too_big:
            response = JSONResponse(status_code=413, content=error_body(413, "The request is too large."))
            response.headers.update(SECURITY_HEADERS)
            return response

    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    # API responses are per-user (attempts, answers, certificates): never
    # let a shared cache or the browser's back/forward cache keep them.
    if request.url.path.startswith("/api/") and request.url.path != "/api/v1/health":
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# CORS is added last so it is the outermost layer: even a 413 or 429
# carries the CORS headers the browser needs to read the error.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    # PUT/DELETE added for the nested-content admin endpoints
    # (answer-key upsert, section/passage/media/question delete).
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    # Lets the browser read the certificate PDF's filename cross-origin.
    expose_headers=["Content-Disposition"],
    max_age=600,
)

register_exception_handlers(app)

app.include_router(v1_router, prefix="/api/v1")

"""Admin audit trail (FR-ADMIN-08, spec section 9 "record sensitive admin
actions").

One middleware covers every admin route, present and future, so no handler
can forget to log. It records:

  * every admin write (POST/PUT/PATCH/DELETE under /admin) - success or
    failure - with the acting admin;
  * sensitive admin reads: exporting a test or a section's questions
    (answer keys included), the user list and the certificate list;
  * every refused attempt to use an admin route by a signed-in non-admin.

The actor comes from `require_admin` (app/core/roles.py), which stores the
verified JWT subject and role on `request.state` - never from anything the
client sends. Unauthenticated calls (401) are not logged: they never reach
the role check, and logging them would let anyone fill the table.

Handlers can add context with `audit_note(request, key=value)`, e.g. the id
of a newly created test or the reason for a revocation.

Writing the log never changes the response: a failure is logged as a
warning and the request carries on.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

from ..db.session import get_session
from ..models import AdminAuditLog
from .rate_limit import RateLimiter

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Reads that expose answer keys or personal data in bulk.
SENSITIVE_READS = {
    "/admin/tests/{test_id}/export",
    "/admin/sections/{section_id}/questions.csv",  # answer keys
    "/admin/users",
    "/admin/certificates",  # students' names and emails
    "/admin/audit",
}

# Refused attempts are logged, but one account can't flood the table.
_denied_limiter = RateLimiter(20, 60, "audit flood guard")


def audit_note(request: Request, **details: Any) -> None:
    """Attach extra context to this request's audit entry."""

    notes = getattr(request.state, "audit_details", None)
    if notes is None:
        notes = {}
        request.state.audit_details = notes
    for key, value in details.items():
        notes[key] = str(value) if isinstance(value, UUID) else value


def _template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    return path[len(API_PREFIX):] if path.startswith(API_PREFIX) else path


def _entity(template: str, path_params: dict[str, Any]) -> tuple[str | None, str | None]:
    """`/admin/tests/{test_id}/publish` -> ("test", "<id>"): the last path
    parameter names the thing acted on."""

    names = [segment[1:-1] for segment in template.split("/") if segment.startswith("{") and segment.endswith("}")]
    if not names:
        parts = [p for p in template.split("/") if p and p != "admin"]
        return (parts[0].rstrip("s") if parts else None), None
    name = names[-1]
    entity_type = name[:-3] if name.endswith("_id") else name
    value = path_params.get(name)
    return entity_type, (str(value)[:100] if value is not None else None)


def _outcome(status_code: int) -> str:
    if status_code < 400:
        return "success"
    if status_code == 403:
        return "denied"
    return "failed"


def _session_factory(app: FastAPI):
    # Tests override get_session with an in-memory database; use the same
    # one here so the log lands where the request's own writes did.
    return app.dependency_overrides.get(get_session, get_session)


def _write(app: FastAPI, entry: AdminAuditLog) -> None:
    generator = _session_factory(app)()
    try:
        session = next(generator)
        session.add(entry)
        session.commit()
    finally:
        generator.close()


def install_audit_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def admin_audit(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            # An unexpected error unwinds through this middleware before the
            # outer generic handler turns it into a 500, so log the failure
            # here, then let the error carry on to that handler.
            await _record(app, request, 500)
            raise
        await _record(app, request, response.status_code)
        return response


async def _record(app: FastAPI, request: Request, status_code: int) -> None:
    if not request.url.path.startswith(f"{API_PREFIX}/admin"):
        return
    actor_id = getattr(request.state, "audit_actor_id", None)
    if actor_id is None:
        return  # never reached the role check (401, 404 route, ...)

    template = _template(request)
    method = request.method.upper()
    outcome = _outcome(status_code)
    if method not in WRITE_METHODS and template not in SENSITIVE_READS and outcome != "denied":
        return
    if outcome == "denied":
        try:
            _denied_limiter.check(actor_id)
        except Exception:
            return

    entity_type, entity_id = _entity(template, dict(request.path_params))
    details = dict(getattr(request.state, "audit_details", None) or {})
    if request.url.query:
        details.setdefault("query", request.url.query[:300])
    try:
        actor_uuid = UUID(actor_id)
    except ValueError:
        actor_uuid = None
        details["actor"] = str(actor_id)[:100]

    entry = AdminAuditLog(
        id=uuid4(),
        actor_id=actor_uuid,
        actor_role=getattr(request.state, "audit_actor_role", None),
        action=f"{method} {template}"[:200],
        method=method,
        path=request.url.path[:500],
        entity_type=entity_type,
        entity_id=entity_id,
        outcome=outcome,
        status_code=status_code,
        details=details,
        created_at=datetime.now(timezone.utc),
    )
    try:
        await run_in_threadpool(_write, app, entry)
    except Exception:  # noqa: BLE001 - logging must never break the request
        logger.warning("Could not write admin audit entry for %s %s", method, template, exc_info=True)


def reset_audit_limits() -> None:
    _denied_limiter.reset()

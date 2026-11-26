from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlmodel import Session

from ...db.session import get_session

router = APIRouter()


# HEAD is allowed too: uptime monitors (UptimeRobot) probe with HEAD by
# default, and a GET-only route answers 405, which they report as "down".
@router.api_route("/health", methods=["GET", "HEAD"])
def health() -> dict:
    """Liveness check. No auth, no DB, no secrets - safe to call unauthenticated."""

    return {"status": "ok"}


@router.api_route("/health/ready", methods=["GET", "HEAD"])
def ready(session: Session = Depends(get_session)) -> dict:
    """Readiness: can this instance reach the database right now? Used by
    the frontend's "waking up the server" check and the deploy smoke test.
    Reports only up/down - never connection details."""

    try:
        session.exec(text("select 1"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="The database is not reachable right now.") from exc
    return {"status": "ok", "database": "ok"}

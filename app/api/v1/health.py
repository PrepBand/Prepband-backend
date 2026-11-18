from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict:
    """Liveness check. No auth, no DB, no secrets - safe to call unauthenticated."""

    return {"status": "ok"}

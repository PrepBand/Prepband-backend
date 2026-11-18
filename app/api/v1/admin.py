from fastapi import APIRouter, Depends

from ...core.roles import require_admin

router = APIRouter()


@router.get("/admin/ping")
def admin_ping(role: str = Depends(require_admin)) -> dict:
    """Proof that admin/student separation actually works end-to-end.

    The real admin CMS (content CRUD, user overview, etc. - FR-ADMIN-*) is
    not built yet; this route only exists to demonstrate and exercise the
    require_admin gate so Phase 1's "student/admin separation proven" exit
    criterion has something real to point at.
    """

    return {"status": "ok", "role": role}

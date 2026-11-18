from fastapi import Depends, HTTPException
from sqlmodel import Session

from ..db.session import get_session
from ..models import UserRole
from .security import get_current_user_id


def get_current_role(
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> str:
    role_row = session.get(UserRole, user_id)
    return role_row.role if role_row else "student"


def require_admin(role: str = Depends(get_current_role)) -> str:
    """Gate for admin-only routes.

    Per spec section 9: admin role can never be self-assigned from the
    client - user_roles has no client-writable insert/update policy at all,
    so the only way this check passes is a row set directly via SQL.
    """

    if role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin role required.")
    return role

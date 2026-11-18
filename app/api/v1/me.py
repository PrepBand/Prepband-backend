from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from ...core.security import get_current_user_id
from ...db.session import get_session
from ...models import Profile, UserRole
from ...schemas.profile import ProfileOut, ProfileUpdate

router = APIRouter()


def _load_profile_and_role(session: Session, user_id: str) -> tuple[Profile, str]:
    # get_current_user_id returns the JWT `sub` claim as a plain str;
    # Profile.id / UserRole.user_id are typed UUID (see models.py), so this
    # must be parsed before querying.
    user_uuid = UUID(user_id)
    profile = session.get(Profile, user_uuid)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found.")
    role_row = session.get(UserRole, user_uuid)
    role = role_row.role if role_row else "student"
    return profile, role


def _to_profile_out(profile: Profile, role: str) -> ProfileOut:
    data = profile.model_dump()
    data["id"] = str(data["id"])
    return ProfileOut(**data, role=role)


@router.get("/me", response_model=ProfileOut)
def get_me(
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> ProfileOut:
    profile, role = _load_profile_and_role(session, user_id)
    return _to_profile_out(profile, role)


@router.patch("/me", response_model=ProfileOut)
def update_me(
    body: ProfileUpdate,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> ProfileOut:
    profile, role = _load_profile_and_role(session, user_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return _to_profile_out(profile, role)

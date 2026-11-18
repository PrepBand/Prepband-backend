from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class ProfileOut(BaseModel):
    id: str
    display_name: str | None = None
    track: Literal["academic", "general"] | None = None
    target_band: float | None = None
    timezone: str | None = None
    exam_date: date | None = None
    avatar_initials: str | None = None
    avatar_url: str | None = None
    email: str
    onboarding_completed: bool
    role: str


class ProfileUpdate(BaseModel):
    """Only the fields FR-AUTH-05 allows a student to change via this API.

    email / avatar_url / onboarding_completed are deliberately excluded -
    each already has its own authoritative write path (auth triggers, the
    upload-avatar Edge Function, the onboarding flow) and must not become
    overwritable through a generic profile PATCH.
    """

    display_name: str | None = Field(default=None, max_length=120)
    track: Literal["academic", "general"] | None = None
    target_band: float | None = Field(default=None, ge=0, le=9)
    timezone: str | None = Field(default=None, max_length=64)
    exam_date: date | None = None

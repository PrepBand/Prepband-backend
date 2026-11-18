"""SQLModel mappings for tables that already exist in Supabase.

Schema (tables, columns, RLS, triggers) is owned by Supabase migrations,
applied via the Supabase MCP and mirrored in
prepband-frontend/src/lib/database.types.ts - never call
SQLModel.metadata.create_all() against these models from this backend.
"""

from datetime import date, datetime

from sqlmodel import Field, SQLModel


class Profile(SQLModel, table=True):
    __tablename__ = "profiles"

    id: str = Field(primary_key=True)
    display_name: str | None = None
    track: str | None = None
    target_band: float | None = None
    timezone: str | None = None
    exam_date: date | None = None
    avatar_initials: str | None = None
    avatar_url: str | None = None
    email: str
    email_confirmed_at: datetime | None = None
    onboarding_completed: bool = False
    created_at: datetime
    updated_at: datetime


class UserRole(SQLModel, table=True):
    __tablename__ = "user_roles"

    user_id: str = Field(primary_key=True)
    role: str = "student"
    created_at: datetime
    updated_at: datetime

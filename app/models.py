"""SQLModel mappings for tables that already exist in Supabase.

Schema (tables, columns, RLS, triggers) is owned by Supabase migrations,
applied via the Supabase MCP and mirrored in
prepband-frontend/src/lib/database.types.ts - never call
SQLModel.metadata.create_all() against these models from this backend.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ARRAY, BigInteger, Text
from sqlmodel import JSON, Column, Field, SQLModel


class Profile(SQLModel, table=True):
    __tablename__ = "profiles"

    # profiles.id is a Postgres `uuid` column (FK to auth.users.id). Must be
    # the Python `UUID` type, not `str` - a `str` field binds as ::VARCHAR,
    # which Postgres can't compare against `uuid` (confirmed live: caused a
    # 500 "operator does not exist: uuid = character varying" on every
    # /me and /admin/ping call).
    id: UUID = Field(primary_key=True)
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

    # Same `uuid` vs. `str` fix as Profile.id above.
    user_id: UUID = Field(primary_key=True)
    role: str = "student"
    created_at: datetime
    updated_at: datetime


# --- Phase 2, Step 1 content model -----------------------------------
#
# Mirrors database/migrations/0001_content_model.sql and 0002's follow-up,
# both applied to the live project 2026-11-19. Same `uuid`-typed-as-UUID
# rule as Profile/UserRole above applies to every FK here.


class Test(SQLModel, table=True):
    __tablename__ = "tests"

    id: UUID = Field(primary_key=True)
    title: str
    module: str
    track: str
    difficulty: str | None = None
    duration_minutes: int | None = None
    status: str = "draft"
    kind: str = "practice"
    current_version_id: UUID | None = None
    created_by: UUID | None = None
    created_at: datetime
    updated_at: datetime


class TestVersion(SQLModel, table=True):
    __tablename__ = "test_versions"

    id: UUID = Field(primary_key=True)
    test_id: UUID
    version_number: int
    status: str = "draft"
    published_at: datetime | None = None
    created_at: datetime


class Section(SQLModel, table=True):
    __tablename__ = "sections"

    id: UUID = Field(primary_key=True)
    test_version_id: UUID
    order_index: int
    module: str
    instructions: str | None = None
    timing_seconds: int | None = None
    created_at: datetime


class Passage(SQLModel, table=True):
    __tablename__ = "passages"

    id: UUID = Field(primary_key=True)
    section_id: UUID
    title: str | None = None
    body: str
    source: str | None = None
    license: str | None = None
    attribution: str | None = None
    created_at: datetime


class MediaAsset(SQLModel, table=True):
    __tablename__ = "media_assets"

    id: UUID = Field(primary_key=True)
    section_id: UUID | None = None
    type: str
    storage_provider: str = "cloudflare_r2"
    storage_key: str
    public_url: str | None = None
    content_type: str | None = None
    duration_seconds: float | None = None
    size_bytes: int | None = None
    attribution: str | None = None
    license: str | None = None
    transcript: str | None = None
    created_at: datetime


class Question(SQLModel, table=True):
    __tablename__ = "questions"

    id: UUID = Field(primary_key=True)
    section_id: UUID
    type: str
    prompt: str
    options: dict[str, Any] | list[Any] | None = Field(default=None, sa_column=Column(JSON))
    display_order: int
    marks: float = 1
    created_at: datetime


class AnswerKey(SQLModel, table=True):
    __tablename__ = "answer_keys"

    id: UUID = Field(primary_key=True)
    question_id: UUID = Field(unique=True)
    correct_answers: dict[str, Any] | list[Any] = Field(sa_column=Column(JSON))
    accepted_aliases: dict[str, Any] | list[Any] | None = Field(default=None, sa_column=Column(JSON))
    rationale: str | None = None
    created_at: datetime


# --- Phase 2, Step 2: the shared test-taking engine --------------------
#
# Mirrors database/migrations/0003_attempts_engine.sql.


class Attempt(SQLModel, table=True):
    __tablename__ = "attempts"

    id: UUID = Field(primary_key=True)
    student_id: UUID
    test_version_id: UUID
    status: str = "in_progress"
    mode: str = "practice"
    started_at: datetime
    expires_at: datetime | None = None
    submitted_at: datetime | None = None
    raw_score: float | None = None
    max_score: float | None = None
    estimated_band: float | None = None
    assessment_status: str = "objective"
    # 0012: marks per question ({question_id: marks}), frozen at submit so
    # the results page always matches the saved score.
    question_marks: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime
    updated_at: datetime


class Response(SQLModel, table=True):
    __tablename__ = "responses"

    id: UUID = Field(primary_key=True)
    attempt_id: UUID
    question_id: UUID
    response: dict[str, Any] | list[Any] | str | float | bool = Field(sa_column=Column(JSON))
    version: int = 1
    saved_at: datetime
    # 0011: the browser's change number; older saves are ignored.
    client_seq: int | None = Field(default=None, sa_column=Column(BigInteger))


# --- Phase 3: Writing and Speaking --------------------------------------
#
# Mirrors database/migrations/0006_writing_speaking.sql.


class WritingPrompt(SQLModel, table=True):
    __tablename__ = "writing_prompts"

    id: UUID = Field(primary_key=True)
    section_id: UUID
    task_number: int
    prompt: str
    instructions: str | None = None
    min_words: int = 150
    recommended_minutes: int | None = None
    visual: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    letter_format: bool = False
    examiner_notes: str | None = None
    display_order: int
    created_at: datetime


class SpeakingPrompt(SQLModel, table=True):
    __tablename__ = "speaking_prompts"

    id: UUID = Field(primary_key=True)
    section_id: UUID
    part: int
    prompt: str
    bullets: list[Any] | None = Field(default=None, sa_column=Column(JSON))
    prep_seconds: int | None = None
    speak_seconds: int | None = None
    display_order: int
    created_at: datetime
    # Phase 5 (0008): admin-written follow-ups [{text, audio_url}] and an
    # optional pre-generated examiner voice clip.
    follow_ups: list[Any] = Field(default_factory=list, sa_column=Column(JSON))
    audio_url: str | None = None


class WritingResponse(SQLModel, table=True):
    __tablename__ = "writing_responses"

    id: UUID = Field(primary_key=True)
    attempt_id: UUID
    writing_prompt_id: UUID
    body: str = ""
    word_count: int = 0
    diagnostics: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    version: int = 1
    saved_at: datetime
    client_seq: int | None = Field(default=None, sa_column=Column(BigInteger))


class SpeakingResponse(SQLModel, table=True):
    __tablename__ = "speaking_responses"

    id: UUID = Field(primary_key=True)
    attempt_id: UUID
    speaking_prompt_id: UUID
    response_mode: str
    duration_seconds: float | None = None
    text_response: str | None = None
    completed: bool = False
    version: int = 1
    saved_at: datetime
    client_seq: int | None = Field(default=None, sa_column=Column(BigInteger))
    # Phase 5 (0008): live mode. transcript/turn transcripts only with consent.
    transcript: str | None = None
    stt_model: str | None = None
    # none_as_null: None must be SQL NULL, not JSON null - the column's check
    # (turns is null or a JSON array) rejects JSON null, which made every
    # audio/text-mode speaking save fail on Postgres.
    turns: list[Any] | None = Field(default=None, sa_column=Column(JSON(none_as_null=True)))


class SelfReview(SQLModel, table=True):
    __tablename__ = "self_reviews"

    attempt_id: UUID = Field(primary_key=True)
    ratings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    checklist: list[Any] = Field(default_factory=list, sa_column=Column(JSON))
    notes: str | None = None
    updated_at: datetime


# --- Phase 4: full mock and certificates --------------------------------
#
# Mirrors database/migrations/0007_full_mock_certificates.sql.


class MockAttempt(SQLModel, table=True):
    __tablename__ = "mock_attempts"

    id: UUID = Field(primary_key=True)
    student_id: UUID
    track: str
    status: str = "in_progress"
    listening_version_id: UUID
    reading_version_id: UUID
    writing_version_id: UUID
    speaking_version_id: UUID
    listening_attempt_id: UUID | None = None
    reading_attempt_id: UUID | None = None
    writing_attempt_id: UUID | None = None
    speaking_attempt_id: UUID | None = None
    started_at: datetime
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class Certificate(SQLModel, table=True):
    __tablename__ = "certificates"

    id: UUID = Field(primary_key=True)
    code: str = Field(unique=True)
    mock_attempt_id: UUID = Field(unique=True)
    student_id: UUID
    issued_at: datetime
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    public_verification: bool = False
    created_at: datetime
    updated_at: datetime


class ExaminerPhrase(SQLModel, table=True):
    """Phase 5 (0008): the live AI practice examiner's fixed lines."""

    __tablename__ = "examiner_phrases"

    key: str = Field(primary_key=True)
    text: str
    audio_url: str | None = None
    updated_at: datetime


class AdminAuditLog(SQLModel, table=True):
    """Phase 6 (0009): append-only record of admin writes and refused admin
    access (FR-ADMIN-08). Written only by app/services/audit.py."""

    __tablename__ = "admin_audit_log"

    id: UUID = Field(primary_key=True)
    actor_id: UUID | None = None
    actor_role: str | None = None
    action: str
    method: str
    path: str
    entity_type: str | None = None
    entity_id: str | None = None
    outcome: str
    status_code: int
    details: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime


# --- Resource library (0014) ---------------------------------------------
#
# Mirrors database/migrations/0014_resource_library.sql. `search` (a
# tsvector kept by a trigger) isn't mapped: the API reads it through SQL.

# text[] on Postgres, JSON on the SQLite test database.
_TAGS_TYPE = ARRAY(Text).with_variant(JSON(), "sqlite")


class ResourceCategory(SQLModel, table=True):
    __tablename__ = "resource_categories"

    id: UUID = Field(primary_key=True)
    name: str
    slug: str = Field(unique=True)
    description: str | None = None
    display_order: int = 0
    is_visible: bool = True
    created_at: datetime
    updated_at: datetime


class Resource(SQLModel, table=True):
    __tablename__ = "resources"

    id: UUID = Field(primary_key=True)
    category_id: UUID
    title: str
    description: str | None = None
    kind: str
    module: str = "general"
    track: str = "both"
    level: str | None = None
    tags: list[str] = Field(default_factory=list, sa_column=Column(_TAGS_TYPE, nullable=False))
    cover_key: str | None = None
    cover_alt: str | None = None
    file_key: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, sa_column=Column(BigInteger))
    external_url: str | None = None
    source: str | None = None
    licence: str | None = None
    status: str = "draft"
    display_order: int = 0
    open_count: int = 0
    created_by: UUID | None = None
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

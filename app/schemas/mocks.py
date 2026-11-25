"""Full mock (FR-MOCK-*) and certificate (FR-CERT-*) request/response shapes."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .attempts import AssessmentStatus, AttemptMode, AttemptStatus
from .content import Module

MockTrack = Literal["academic", "general"]
MockStatus = Literal["in_progress", "completed", "abandoned"]
ModuleProgressStatus = Literal["not_started", "in_progress", "submitted", "expired", "cancelled"]


class MockCreate(BaseModel):
    track: MockTrack


class MockModuleOut(BaseModel):
    module: Module
    test_id: str
    test_title: str
    duration_minutes: int | None
    attempt_id: str | None
    status: ModuleProgressStatus
    started_at: datetime | None
    expires_at: datetime | None
    submitted_at: datetime | None
    # True when this is the module the student should take next.
    is_next: bool


class CertificateSummary(BaseModel):
    id: str
    code: str
    issued_at: datetime
    revoked_at: datetime | None
    public_verification: bool


class MockOut(BaseModel):
    id: str
    track: MockTrack
    status: MockStatus
    started_at: datetime
    completed_at: datetime | None
    modules: list[MockModuleOut]
    completed_modules: int
    next_module: Module | None
    certificate: CertificateSummary | None


class MockModuleResult(BaseModel):
    module: Module
    test_title: str
    attempt_id: str | None
    status: ModuleProgressStatus
    mode: AttemptMode | None
    # FR-RESULT-04 provenance: 'objective' scores are machine-marked against
    # an answer key; 'not_assessed' means no score exists at all.
    assessment_status: AssessmentStatus
    raw_score: float | None
    max_score: float | None
    estimated_band: float | None
    submitted_at: datetime | None
    words_written: int | None = None
    tasks_answered: int | None = None
    tasks_total: int | None = None
    speaking_answers: int | None = None
    speaking_prompts_total: int | None = None


class MockResultsOut(BaseModel):
    id: str
    track: MockTrack
    status: MockStatus
    started_at: datetime
    completed_at: datetime | None
    modules: list[MockModuleResult]
    # FR-MOCK-06: null whenever any module has no valid band (always the
    # case while Writing and Speaking are unassessed).
    overall_band: float | None
    overall_band_note: str
    band_note: str
    certificate: CertificateSummary | None


class MockAttemptStatusOut(BaseModel):
    """Returned by POST /mocks/{id}/modules/{module}/start."""

    mock_id: str
    module: Module
    attempt_id: str
    test_id: str
    status: AttemptStatus


# --- certificates ----------------------------------------------------------


class CertificateOut(CertificateSummary):
    mock_attempt_id: str
    track: MockTrack
    completed_at: datetime | None
    revoked_reason: str | None
    # Only present while the holder has opted into public verification.
    verification_url: str | None


class CertificateDetail(CertificateOut):
    holder_name: str
    modules: list[MockModuleResult]
    overall_band: float | None
    overall_band_note: str


class CertificateUpdate(BaseModel):
    public_verification: bool


class VerificationOut(BaseModel):
    """FR-CERT-06: the minimum a public verifier sees. No name, no scores."""

    code: str
    status: Literal["valid", "revoked"]
    issued_on: date
    statement: str


class CertificateRevoke(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class AdminCertificateOut(CertificateOut):
    student_id: str
    holder_name: str
    holder_email: str | None

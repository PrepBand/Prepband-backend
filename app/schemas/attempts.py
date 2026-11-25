from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .content import FollowUp, Module, QuestionType, TestKind, Track

AttemptStatus = Literal["in_progress", "submitted", "expired", "cancelled"]
AttemptMode = Literal["practice", "exam"]
AssessmentStatus = Literal["objective", "not_assessed"]


# --- catalog / safe content (never includes answer_keys) ---------------


class PassagePublic(BaseModel):
    id: str
    title: str | None
    body: str
    source: str | None
    license: str | None
    attribution: str | None


class MediaAssetPublic(BaseModel):
    id: str
    type: Literal["audio", "image"]
    public_url: str | None
    content_type: str | None
    duration_seconds: float | None
    attribution: str | None
    license: str | None


class QuestionPublic(BaseModel):
    id: str
    type: QuestionType
    prompt: str
    options: Any | None
    display_order: int
    marks: float


class SectionPublic(BaseModel):
    id: str
    module: Module
    order_index: int
    instructions: str | None
    timing_seconds: int | None
    passages: list[PassagePublic]
    media_assets: list[MediaAssetPublic]
    questions: list[QuestionPublic]
    writing_prompts: list["WritingPromptPublic"] = []
    speaking_prompts: list["SpeakingPromptPublic"] = []


class WritingPromptPublic(BaseModel):
    """examiner_notes is deliberately absent - it only appears in results."""

    id: str
    task_number: int
    prompt: str
    instructions: str | None
    min_words: int
    recommended_minutes: int | None
    visual: Any | None
    letter_format: bool
    display_order: int


class SpeakingPromptPublic(BaseModel):
    id: str
    part: int
    prompt: str
    bullets: list[str] | None
    prep_seconds: int | None
    speak_seconds: int | None
    display_order: int
    # Phase 5: the live examiner's follow-ups and optional voice clip.
    follow_ups: list[FollowUp] = []
    audio_url: str | None = None


class TestCatalogItem(BaseModel):
    id: str
    title: str
    module: Module
    track: Track
    kind: TestKind
    difficulty: str | None
    duration_minutes: int | None


class TestDetail(TestCatalogItem):
    sections: list[SectionPublic]


# --- attempts ------------------------------------------------------------


class AttemptCreate(BaseModel):
    test_id: str
    mode: AttemptMode = "practice"


class AttemptOut(BaseModel):
    id: str
    test_version_id: str
    status: AttemptStatus
    mode: AttemptMode
    started_at: datetime
    expires_at: datetime | None
    submitted_at: datetime | None
    raw_score: float | None
    max_score: float | None
    estimated_band: float | None
    assessment_status: AssessmentStatus = "objective"


class ResponseUpsert(BaseModel):
    response: Any
    # Change number from the browser; a save older than the stored one is ignored.
    client_seq: int | None = Field(default=None, ge=0, le=2**62)


class ResponseOut(BaseModel):
    question_id: str
    response: Any
    version: int
    saved_at: datetime
    client_seq: int | None = None


class WritingResponseUpsert(BaseModel):
    body: str = Field(default="", max_length=20000)
    # Change number from the browser; a save older than the stored one is ignored.
    client_seq: int | None = Field(default=None, ge=0, le=2**62)


class WritingResponseOut(BaseModel):
    writing_prompt_id: str
    body: str
    word_count: int
    version: int
    saved_at: datetime
    client_seq: int | None = None


class LiveTurn(BaseModel):
    """One examiner question in live mode (the main prompt, a follow-up or
    a repeat) and how the student answered it."""

    kind: Literal["question", "follow_up", "repeat", "more"]
    text: str = Field(min_length=1, max_length=500)
    duration_seconds: float = Field(ge=0, le=600)
    long_pauses: int = Field(default=0, ge=0, le=500)
    ended_by: Literal["silence", "time", "button", "no_speech"] = "silence"
    transcript: str | None = Field(default=None, max_length=5000)
    stt_model: str | None = Field(default=None, max_length=80)


class SpeakingResponseUpsert(BaseModel):
    response_mode: Literal["audio", "text", "live"]
    duration_seconds: float | None = Field(default=None, ge=0, le=3600)
    text_response: str | None = Field(default=None, max_length=10000)
    completed: bool = False
    # Live mode (FR-LIVE-11): transcripts are kept only when the student
    # ticked "save my transcript"; otherwise the API drops them.
    save_transcript: bool = False
    transcript: str | None = Field(default=None, max_length=20000)
    stt_model: str | None = Field(default=None, max_length=80)
    turns: list[LiveTurn] | None = Field(default=None, max_length=20)
    # Change number from the browser; a save older than the stored one is ignored.
    client_seq: int | None = Field(default=None, ge=0, le=2**62)


class SpeakingResponseOut(BaseModel):
    speaking_prompt_id: str
    response_mode: Literal["audio", "text", "live"]
    duration_seconds: float | None
    text_response: str | None
    completed: bool
    version: int
    saved_at: datetime
    client_seq: int | None = None
    transcript: str | None = None
    stt_model: str | None = None
    turns: list[LiveTurn] | None = None


class AttemptWithResponses(AttemptOut):
    responses: list[ResponseOut]
    writing_responses: list[WritingResponseOut] = []
    speaking_responses: list[SpeakingResponseOut] = []


class ResultQuestion(BaseModel):
    question_id: str
    type: QuestionType
    prompt: str
    your_answer: Any | None
    correct_answers: Any
    accepted_aliases: Any | None
    is_correct: bool
    marks_awarded: float
    marks_available: float
    rationale: str | None


class MediaTranscriptOut(BaseModel):
    media_asset_id: str
    type: Literal["audio", "image"]
    transcript: str | None


class WritingResult(BaseModel):
    writing_prompt_id: str
    task_number: int
    prompt: str
    min_words: int
    body: str
    word_count: int
    diagnostics: dict[str, Any] | None
    examiner_notes: str | None


class SpeakingResult(BaseModel):
    speaking_prompt_id: str
    part: int
    prompt: str
    bullets: list[str] | None
    response_mode: Literal["audio", "text", "live"] | None
    duration_seconds: float | None
    text_response: str | None
    completed: bool
    transcript: str | None = None
    stt_model: str | None = None
    turns: list[LiveTurn] | None = None


class SelfReviewIn(BaseModel):
    ratings: dict[str, int] = {}
    checklist: list[str] = []
    notes: str | None = Field(default=None, max_length=5000)

    @field_validator("ratings")
    @classmethod
    def _ratings_in_range(cls, value: dict[str, int]) -> dict[str, int]:
        for key, rating in value.items():
            if len(key) > 60 or not 1 <= rating <= 9:
                raise ValueError("Each self-rating must be between 1 and 9.")
        return value


class SelfReviewOut(BaseModel):
    ratings: dict[str, int]
    checklist: list[str]
    notes: str | None
    updated_at: datetime


class ResultsOut(AttemptOut):
    module: Module
    questions: list[ResultQuestion]
    media_transcripts: list[MediaTranscriptOut]
    writing: list[WritingResult] = []
    speaking: list[SpeakingResult] = []
    self_review: SelfReviewOut | None = None


class AttemptHistoryItem(BaseModel):
    """GET /me/attempts - the student's own history (FR-WRITE-06, FR-SPEAK-06)."""

    id: str
    test_id: str
    test_title: str
    module: Module
    track: Track
    kind: TestKind
    status: AttemptStatus
    mode: AttemptMode
    assessment_status: AssessmentStatus
    started_at: datetime
    submitted_at: datetime | None
    raw_score: float | None
    max_score: float | None
    estimated_band: float | None
    words_written: int | None = None
    speaking_answers: int | None = None
    # Set when the attempt is one module of a full mock.
    mock_attempt_id: str | None = None


class AttemptHistoryPage(BaseModel):
    items: list[AttemptHistoryItem]
    total: int
    limit: int
    offset: int


class ProgressPoint(BaseModel):
    attempt_id: str
    module: Module
    test_title: str
    kind: TestKind
    started_at: datetime
    submitted_at: datetime
    assessment_status: AssessmentStatus
    raw_score: float | None
    max_score: float | None
    percentage: float | None
    estimated_band: float | None
    words_written: int | None = None
    speaking_answers: int | None = None


class ProgressOut(BaseModel):
    completed_by_module: dict[str, int]
    points: list[ProgressPoint]
    full_mocks_completed: int
    certificates: int


SectionPublic.model_rebuild()

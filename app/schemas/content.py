from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Module = Literal["listening", "reading", "writing", "speaking"]
Track = Literal["academic", "general", "both"]
TestStatus = Literal["draft", "published", "archived"]
TestKind = Literal["practice", "mock"]
MediaType = Literal["audio", "image"]
QuestionType = Literal[
    "multiple_choice",
    "multiple_select",
    "true_false_not_given",
    "yes_no_not_given",
    "matching",
    "short_answer",
    "sentence_completion",
    "gap_fill",
    "form_completion",
    "note_completion",
    "table_completion",
]


class TestCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    module: Module
    track: Track
    kind: TestKind = "practice"
    difficulty: str | None = Field(default=None, max_length=40)
    duration_minutes: int | None = Field(default=None, ge=1)


class TestUpdate(BaseModel):
    """Only metadata - editing real content (sections/questions/etc.) is a
    later admin-authoring step, not part of this endpoint."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    track: Track | None = None
    kind: TestKind | None = None
    difficulty: str | None = Field(default=None, max_length=40)
    duration_minutes: int | None = Field(default=None, ge=1)


class TestOut(BaseModel):
    id: str
    title: str
    module: Module
    track: Track
    kind: TestKind
    difficulty: str | None
    duration_minutes: int | None
    status: TestStatus
    current_version_id: str | None
    created_at: datetime
    updated_at: datetime


class AdminUserOut(BaseModel):
    id: str
    email: str
    display_name: str | None
    role: str
    onboarding_completed: bool
    created_at: datetime
    attempts_total: int = 0
    attempts_submitted: int = 0


class AdminOverviewOut(BaseModel):
    total_users: int
    admins: int
    tests_by_status: dict[str, int]
    published_by_module: dict[str, int]
    attempts_by_status: dict[str, int]


# --- Nested content: sections / passages / media assets / questions /
# answer keys. All of these only exist under a test_version, and every
# create/update/delete below is rejected with 409 once that version is
# published - see admin_content.py's _require_draft_section helper.


class SectionCreate(BaseModel):
    module: Module
    order_index: int = Field(ge=0)
    instructions: str | None = None
    timing_seconds: int | None = Field(default=None, ge=0)


class SectionUpdate(BaseModel):
    module: Module | None = None
    order_index: int | None = Field(default=None, ge=0)
    instructions: str | None = None
    timing_seconds: int | None = Field(default=None, ge=0)


class SectionOut(BaseModel):
    id: str
    test_version_id: str
    module: Module
    order_index: int
    instructions: str | None
    timing_seconds: int | None
    created_at: datetime


class PassageCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1)
    source: str | None = None
    license: str | None = None
    attribution: str | None = None


class PassageUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, min_length=1)
    source: str | None = None
    license: str | None = None
    attribution: str | None = None


class PassageOut(BaseModel):
    id: str
    section_id: str
    title: str | None
    body: str
    source: str | None
    license: str | None
    attribution: str | None
    created_at: datetime


class MediaAssetCreate(BaseModel):
    """Metadata only - the audio/image bytes are expected to already be in
    the shared prepband-avatars R2 bucket under
    test-audio/<test_version_id>/... or test-images/<test_version_id>/...
    (see 0001_content_model.sql). This endpoint never touches R2 itself."""

    type: MediaType
    storage_key: str = Field(min_length=1)
    public_url: str | None = None
    content_type: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    attribution: str | None = None
    license: str | None = None
    transcript: str | None = None


class MediaAssetUpdate(BaseModel):
    storage_key: str | None = Field(default=None, min_length=1)
    public_url: str | None = None
    content_type: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    attribution: str | None = None
    license: str | None = None
    transcript: str | None = None


class MediaAssetOut(BaseModel):
    id: str
    section_id: str | None
    type: MediaType
    storage_provider: str
    storage_key: str
    public_url: str | None
    content_type: str | None
    duration_seconds: float | None
    size_bytes: int | None
    attribution: str | None
    license: str | None
    transcript: str | None
    created_at: datetime


class QuestionCreate(BaseModel):
    type: QuestionType
    prompt: str = Field(min_length=1)
    options: Any | None = None
    display_order: int = Field(ge=0)
    marks: float = Field(default=1, ge=0)


class QuestionUpdate(BaseModel):
    type: QuestionType | None = None
    prompt: str | None = Field(default=None, min_length=1)
    options: Any | None = None
    display_order: int | None = Field(default=None, ge=0)
    marks: float | None = Field(default=None, ge=0)


class QuestionOut(BaseModel):
    id: str
    section_id: str
    type: QuestionType
    prompt: str
    options: Any | None
    display_order: int
    marks: float
    created_at: datetime


class AnswerKeyUpsert(BaseModel):
    correct_answers: Any
    accepted_aliases: Any | None = None
    rationale: str | None = None


class AnswerKeyOut(BaseModel):
    id: str
    question_id: str
    correct_answers: Any
    accepted_aliases: Any | None
    rationale: str | None
    created_at: datetime


class WritingPromptCreate(BaseModel):
    task_number: Literal[1, 2]
    prompt: str = Field(min_length=1)
    instructions: str | None = None
    min_words: int = Field(default=150, ge=0, le=2000)
    recommended_minutes: int | None = Field(default=None, ge=1, le=180)
    visual: dict[str, Any] | None = None
    letter_format: bool = False
    examiner_notes: str | None = None
    display_order: int = Field(ge=0)


class WritingPromptUpdate(BaseModel):
    task_number: Literal[1, 2] | None = None
    prompt: str | None = Field(default=None, min_length=1)
    instructions: str | None = None
    min_words: int | None = Field(default=None, ge=0, le=2000)
    recommended_minutes: int | None = Field(default=None, ge=1, le=180)
    visual: dict[str, Any] | None = None
    letter_format: bool | None = None
    examiner_notes: str | None = None
    display_order: int | None = Field(default=None, ge=0)


class WritingPromptOut(BaseModel):
    id: str
    section_id: str
    task_number: int
    prompt: str
    instructions: str | None
    min_words: int
    recommended_minutes: int | None
    visual: Any | None
    letter_format: bool
    examiner_notes: str | None
    display_order: int
    created_at: datetime


class FollowUp(BaseModel):
    """Phase 5: a follow-up question the live examiner may ask (FR-LIVE-09)."""

    text: str = Field(min_length=1, max_length=500)
    audio_url: str | None = Field(default=None, pattern=r"^https://", max_length=1000)


AudioUrl = Field(default=None, pattern=r"^https://", max_length=1000)


class SpeakingPromptCreate(BaseModel):
    part: Literal[1, 2, 3]
    prompt: str = Field(min_length=1)
    bullets: list[str] | None = None
    prep_seconds: int | None = Field(default=None, ge=0, le=600)
    speak_seconds: int | None = Field(default=None, ge=1, le=600)
    display_order: int = Field(ge=0)
    follow_ups: list[FollowUp] = Field(default_factory=list, max_length=10)
    audio_url: str | None = AudioUrl


class SpeakingPromptUpdate(BaseModel):
    part: Literal[1, 2, 3] | None = None
    prompt: str | None = Field(default=None, min_length=1)
    bullets: list[str] | None = None
    prep_seconds: int | None = Field(default=None, ge=0, le=600)
    speak_seconds: int | None = Field(default=None, ge=1, le=600)
    display_order: int | None = Field(default=None, ge=0)
    follow_ups: list[FollowUp] | None = Field(default=None, max_length=10)
    audio_url: str | None = AudioUrl


class SpeakingPromptOut(BaseModel):
    id: str
    section_id: str
    part: int
    prompt: str
    bullets: list[str] | None
    prep_seconds: int | None
    speak_seconds: int | None
    display_order: int
    created_at: datetime
    follow_ups: list[FollowUp] = []
    audio_url: str | None = None


class SectionContentOut(SectionOut):
    passages: list[PassageOut]
    media_assets: list[MediaAssetOut]
    questions: list[QuestionOut]
    answer_keys: list[AnswerKeyOut]
    writing_prompts: list[WritingPromptOut] = []
    speaking_prompts: list[SpeakingPromptOut] = []


class AdminTestContentOut(BaseModel):
    test: TestOut
    sections: list[SectionContentOut]
    # Empty when the test is ready to publish (or already published).
    publish_problems: list[str]

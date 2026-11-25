"""FR-TEST-02 -> FR-TEST-08: server-owned attempts, idempotent response
upsert and submit, and grading frozen against the test_version an attempt
started against - never against whatever the test looks like now.
"""

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, text
from sqlmodel import Session, select

from ...core.security import get_current_user_id
from ...db.session import get_session
from ...models import (
    AnswerKey,
    Attempt,
    Certificate,
    MediaAsset,
    MockAttempt,
    Question,
    Response,
    Section,
    SelfReview,
    SpeakingPrompt,
    SpeakingResponse,
    Test,
    TestVersion,
    WritingPrompt,
    WritingResponse,
)
from ...schemas.attempts import (
    AttemptCreate,
    AttemptHistoryItem,
    AttemptHistoryPage,
    AttemptOut,
    AttemptWithResponses,
    MediaTranscriptOut,
    ProgressOut,
    ProgressPoint,
    ResponseOut,
    ResponseUpsert,
    ResultQuestion,
    ResultsOut,
    SelfReviewIn,
    SelfReviewOut,
    SpeakingResponseOut,
    SpeakingResponseUpsert,
    SpeakingResult,
    TestDetail,
    WritingResponseOut,
    WritingResponseUpsert,
    WritingResult,
)
from ...services.band_conversion import estimate_band
from ...services.grading import grade_responses, is_correct, marks_for
from ...services.rate_limit import attempt_start_limiter, autosave_limiter, per_user, submit_limiter
from ...services.writing_diagnostics import count_words, diagnose
from .tests import build_test_detail

# FR-WRITE-07 / FR-SPEAK-08: these modules never get an automated score.
NOT_ASSESSED_MODULES = {"writing", "speaking"}

router = APIRouter()

MODULE_ORDER = ("listening", "reading", "writing", "speaking")


def as_utc(value: datetime | None) -> datetime | None:
    """Timestamps from Postgres are timezone-aware; treat a naive one
    (e.g. from a test database) as UTC rather than failing to compare."""

    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def mock_for_attempt(session: Session, attempt_id: UUID) -> MockAttempt | None:
    """The full mock this module attempt belongs to, if any."""

    return session.exec(
        select(MockAttempt).where(
            or_(
                MockAttempt.listening_attempt_id == attempt_id,
                MockAttempt.reading_attempt_id == attempt_id,
                MockAttempt.writing_attempt_id == attempt_id,
                MockAttempt.speaking_attempt_id == attempt_id,
            )
        )
    ).first()


def mock_ids_by_attempt(session: Session, student_id: UUID) -> dict[UUID, UUID]:
    mapping: dict[UUID, UUID] = {}
    for mock in session.exec(select(MockAttempt).where(MockAttempt.student_id == student_id)).all():
        for module in MODULE_ORDER:
            attempt_id = getattr(mock, f"{module}_attempt_id")
            if attempt_id is not None:
                mapping[attempt_id] = mock.id
    return mapping


# --- shared lookups -----------------------------------------------------


def require_owned_attempt(session: Session, attempt_id: UUID, user_id: UUID, lock: bool = False) -> Attempt:
    """The caller's attempt, or 404. With lock=True the row is locked
    (SELECT ... FOR UPDATE) until the transaction ends, so saves, expiry and
    submit on one attempt run one at a time instead of racing."""

    if lock:
        attempt = session.exec(
            select(Attempt).where(Attempt.id == attempt_id).with_for_update().execution_options(populate_existing=True)
        ).first()
    else:
        attempt = session.get(Attempt, attempt_id)
    if attempt is None or attempt.student_id != user_id:
        raise HTTPException(status_code=404, detail="Attempt not found.")
    return attempt


def _lock(session: Session, attempt: Attempt) -> Attempt:
    """Re-reads an attempt under a row lock, picking up a status another
    request may have just committed."""

    return session.exec(
        select(Attempt).where(Attempt.id == attempt.id).with_for_update().execution_options(populate_existing=True)
    ).one()


def is_stale(row_seq: int | None, incoming_seq: int | None) -> bool:
    """True when a save is older than (or a repeat of) the one already
    stored. The browser numbers every change, so a retried old save that
    arrives late can't overwrite a newer answer."""

    return incoming_seq is not None and row_seq is not None and incoming_seq <= row_seq


# Attempts only run against published versions, whose sections a DB
# trigger keeps from changing, so their ids are safe to cache. This saves a
# query on every autosave. Bounded so it can't grow without limit.
_SECTION_CACHE: dict[UUID, list[UUID]] = {}
_SECTION_CACHE_MAX = 512


def _section_ids_for_version(session: Session, test_version_id: UUID) -> list[UUID]:
    cached = _SECTION_CACHE.get(test_version_id)
    if cached is not None:
        return cached
    sections = session.exec(select(Section).where(Section.test_version_id == test_version_id)).all()
    ids = [section.id for section in sections]
    version = session.get(TestVersion, test_version_id)
    if ids and version is not None and version.status == "published":
        if len(_SECTION_CACHE) >= _SECTION_CACHE_MAX:
            _SECTION_CACHE.clear()
        _SECTION_CACHE[test_version_id] = ids
    return ids


def clear_section_cache() -> None:
    _SECTION_CACHE.clear()


def _media_assets_for_version(session: Session, test_version_id: UUID) -> list[MediaAsset]:
    section_ids = _section_ids_for_version(session, test_version_id)
    if not section_ids:
        return []
    return session.exec(select(MediaAsset).where(MediaAsset.section_id.in_(section_ids))).all()


def _questions_with_keys(session: Session, test_version_id: UUID) -> list[tuple[Question, AnswerKey | None]]:
    section_ids = _section_ids_for_version(session, test_version_id)
    if not section_ids:
        return []
    questions = session.exec(select(Question).where(Question.section_id.in_(section_ids))).all()
    if not questions:
        return []
    question_ids = [q.id for q in questions]
    answer_keys = session.exec(select(AnswerKey).where(AnswerKey.question_id.in_(question_ids))).all()
    answer_key_by_question = {ak.question_id: ak for ak in answer_keys}
    return [(q, answer_key_by_question.get(q.id)) for q in questions]


def _test_for_attempt(session: Session, attempt: Attempt) -> Test:
    version = session.get(TestVersion, attempt.test_version_id)
    test = session.get(Test, version.test_id) if version else None
    if test is None:
        raise HTTPException(status_code=404, detail="Test for this attempt no longer exists.")
    return test


def _writing_prompts(session: Session, test_version_id: UUID) -> list[WritingPrompt]:
    section_ids = _section_ids_for_version(session, test_version_id)
    if not section_ids:
        return []
    return session.exec(
        select(WritingPrompt).where(WritingPrompt.section_id.in_(section_ids)).order_by(WritingPrompt.display_order)
    ).all()


def _speaking_prompts(session: Session, test_version_id: UUID) -> list[SpeakingPrompt]:
    section_ids = _section_ids_for_version(session, test_version_id)
    if not section_ids:
        return []
    return session.exec(
        select(SpeakingPrompt).where(SpeakingPrompt.section_id.in_(section_ids)).order_by(SpeakingPrompt.display_order)
    ).all()


def _finalize(session: Session, attempt: Attempt, status: str, now: datetime) -> Attempt:
    """Grades the attempt against its frozen test_version and transitions
    it to a terminal status. Only ever called while attempt.status is
    still 'in_progress' - the DB trigger (attempts_immutable_once_terminal)
    would reject this once it isn't, as a second line of defense.

    Writing/Speaking attempts are never scored: they get diagnostics
    (Writing) and assessment_status 'not_assessed'. Diagnostics are written
    *before* the status flips, because writing_responses are frozen by a
    trigger as soon as the attempt is terminal."""

    if attempt.assessment_status == "not_assessed":
        prompts = {p.id: p for p in _writing_prompts(session, attempt.test_version_id)}
        for row in session.exec(select(WritingResponse).where(WritingResponse.attempt_id == attempt.id)).all():
            prompt = prompts.get(row.writing_prompt_id)
            if prompt is None:
                continue
            row.word_count = count_words(row.body)
            row.diagnostics = diagnose(row.body, prompt.min_words, prompt.letter_format)
            session.add(row)
        session.flush()

        attempt.status = status
        attempt.submitted_at = now
        attempt.raw_score = None
        attempt.max_score = None
        attempt.estimated_band = None
        session.add(attempt)
        session.commit()
        session.refresh(attempt)
        return attempt

    questions_with_keys = _questions_with_keys(session, attempt.test_version_id)
    responses = session.exec(select(Response).where(Response.attempt_id == attempt.id)).all()
    responses_by_question = {r.question_id: r for r in responses}
    raw_score, max_score = grade_responses(questions_with_keys, responses_by_question)
    # Frozen with the score, in the same update that ends the attempt, so a
    # later change to the marking rules never makes old results disagree
    # with the total the student was given.
    attempt.question_marks = {
        str(question.id): marks_for(question, answer_key, responses_by_question[question.id].response)
        if question.id in responses_by_question
        else 0.0
        for question, answer_key in questions_with_keys
        if answer_key is not None
    }

    attempt.status = status
    attempt.submitted_at = now
    attempt.raw_score = raw_score
    attempt.max_score = max_score
    attempt.estimated_band = estimate_band(raw_score, max_score)
    session.add(attempt)
    session.commit()
    session.refresh(attempt)
    return attempt


def reconcile_expiry(session: Session, attempt: Attempt) -> Attempt:
    """FR-TEST-06/07: the server's own clock is authoritative. If an
    in_progress attempt has run past its deadline, auto-finalize it as
    expired right here - whichever request happens to touch it next
    (a GET, a response save, an explicit submit) is what reconciles it,
    since there's no background worker on this free-tier deployment."""

    if attempt.status != "in_progress" or attempt.expires_at is None:
        return attempt
    now = datetime.now(timezone.utc)
    if now <= as_utc(attempt.expires_at):
        return attempt
    # Two requests can notice the deadline at once (a save and a refresh):
    # lock, then re-check, so only one of them finalizes.
    attempt = _lock(session, attempt)
    if attempt.status != "in_progress":
        return attempt
    return _finalize(session, attempt, "expired", now)


def expire_overdue(session: Session, student_id: UUID) -> None:
    """Finalizes a student's attempts whose time ran out while nobody had
    them open, so lists never show a dead attempt as in progress."""

    now = datetime.now(timezone.utc)
    overdue = session.exec(
        select(Attempt).where(
            Attempt.student_id == student_id,
            Attempt.status == "in_progress",
            Attempt.expires_at.is_not(None),
            Attempt.expires_at < now,
        )
    ).all()
    for attempt in overdue:
        reconcile_expiry(session, attempt)


def to_attempt_out(attempt: Attempt) -> AttemptOut:
    return AttemptOut(
        id=str(attempt.id),
        test_version_id=str(attempt.test_version_id),
        status=attempt.status,
        mode=attempt.mode,
        started_at=attempt.started_at,
        expires_at=attempt.expires_at,
        submitted_at=attempt.submitted_at,
        raw_score=attempt.raw_score,
        max_score=attempt.max_score,
        estimated_band=attempt.estimated_band,
        assessment_status=attempt.assessment_status,
    )


# --- POST /attempts ------------------------------------------------------


@router.post("/attempts", response_model=AttemptOut, dependencies=[Depends(per_user(attempt_start_limiter))])
def create_attempt(
    body: AttemptCreate,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> AttemptOut:
    student_id = UUID(user_id)
    test = session.get(Test, UUID(body.test_id))
    if test is None or test.status != "published" or test.current_version_id is None:
        raise HTTPException(status_code=404, detail="Published test not found.")

    # FR-TEST-05: no duplicate attempt on reconnect - resume the existing
    # one if there already is an in_progress attempt for this version.
    # Attempts that belong to a full mock are resumed through /mocks only.
    candidates = session.exec(
        select(Attempt).where(
            Attempt.student_id == student_id,
            Attempt.test_version_id == test.current_version_id,
            Attempt.status == "in_progress",
        )
    ).all()
    for existing in candidates:
        if mock_for_attempt(session, existing.id) is not None:
            continue
        existing = reconcile_expiry(session, existing)
        if existing.status == "in_progress":
            return to_attempt_out(existing)

    # Two simultaneous starts could both get here and create two attempts.
    # Serialize starts per student (the lock ends with the commit below),
    # then look again for an attempt the other request just created.
    _lock_attempt_starts(session, student_id)
    existing = _open_standalone_attempt(session, student_id, test.current_version_id)
    if existing is not None:
        return to_attempt_out(existing)

    attempt = new_attempt(student_id, test, test.current_version_id, body.mode)
    session.add(attempt)
    session.commit()
    session.refresh(attempt)
    return to_attempt_out(attempt)


def _lock_attempt_starts(session: Session, student_id: UUID) -> None:
    """A transaction-scoped Postgres advisory lock per student. (SQLite,
    used by the unit tests, runs one writer at a time anyway.)"""

    if session.get_bind().dialect.name != "postgresql":
        return
    session.exec(
        text("select pg_advisory_xact_lock(hashtextextended(:key, 0))").bindparams(key=f"attempt-start:{student_id}")
    )


def _open_standalone_attempt(session: Session, student_id: UUID, version_id: UUID) -> Attempt | None:
    """The student's open attempt on this version that isn't part of a mock."""

    candidates = session.exec(
        select(Attempt)
        .where(
            Attempt.student_id == student_id,
            Attempt.test_version_id == version_id,
            Attempt.status == "in_progress",
        )
        .execution_options(populate_existing=True)
    ).all()
    return next((a for a in candidates if mock_for_attempt(session, a.id) is None), None)


def new_attempt(student_id: UUID, test: Test, version_id: UUID, mode: str) -> Attempt:
    """An unsaved in_progress attempt; the deadline comes from the test's duration."""

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=test.duration_minutes) if test.duration_minutes is not None else None
    return Attempt(
        id=uuid4(),
        student_id=student_id,
        test_version_id=version_id,
        status="in_progress",
        mode=mode,
        assessment_status="not_assessed" if test.module in NOT_ASSESSED_MODULES else "objective",
        started_at=now,
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )


# --- GET /attempts/{id} --------------------------------------------------


@router.get("/attempts/{attempt_id}", response_model=AttemptWithResponses)
def get_attempt(
    attempt_id: UUID,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> AttemptWithResponses:
    attempt = require_owned_attempt(session, attempt_id, UUID(user_id))
    attempt = reconcile_expiry(session, attempt)

    responses = session.exec(select(Response).where(Response.attempt_id == attempt.id)).all()
    writing = session.exec(select(WritingResponse).where(WritingResponse.attempt_id == attempt.id)).all()
    speaking = session.exec(select(SpeakingResponse).where(SpeakingResponse.attempt_id == attempt.id)).all()
    base = to_attempt_out(attempt)
    return AttemptWithResponses(
        **base.model_dump(),
        responses=[
            _to_response_out(r)
            for r in responses
        ],
        writing_responses=[_to_writing_out(w) for w in writing],
        speaking_responses=[_to_speaking_out(sp) for sp in speaking],
    )


# --- GET /attempts/{id}/content ------------------------------------------


@router.get("/attempts/{attempt_id}/content", response_model=TestDetail)
def get_attempt_content(
    attempt_id: UUID,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> TestDetail:
    """The student-safe content of the exact version this attempt was
    started against - stays correct even if the test is republished or
    archived while the attempt is open."""

    attempt = require_owned_attempt(session, attempt_id, UUID(user_id))
    test = _test_for_attempt(session, attempt)
    return build_test_detail(session, test, attempt.test_version_id)


def _to_writing_out(row: WritingResponse) -> WritingResponseOut:
    return WritingResponseOut(
        writing_prompt_id=str(row.writing_prompt_id),
        body=row.body,
        word_count=row.word_count,
        version=row.version,
        saved_at=row.saved_at,
        client_seq=row.client_seq,
    )


def _to_speaking_out(row: SpeakingResponse) -> SpeakingResponseOut:
    return SpeakingResponseOut(
        speaking_prompt_id=str(row.speaking_prompt_id),
        response_mode=row.response_mode,
        duration_seconds=row.duration_seconds,
        text_response=row.text_response,
        completed=row.completed,
        version=row.version,
        saved_at=row.saved_at,
        client_seq=row.client_seq,
        transcript=row.transcript,
        stt_model=row.stt_model,
        turns=row.turns,
    )


def _require_open_attempt(session: Session, attempt_id: UUID, user_id: str) -> Attempt:
    attempt = require_owned_attempt(session, attempt_id, UUID(user_id), lock=True)
    attempt = reconcile_expiry(session, attempt)
    if attempt.status != "in_progress":
        raise HTTPException(status_code=409, detail=f"Attempt is {attempt.status} and no longer accepting answers.")
    return attempt


# --- PUT /attempts/{id}/writing/{promptId} (FR-WRITE-03) ------------------


@router.put("/attempts/{attempt_id}/writing/{prompt_id}", response_model=WritingResponseOut, dependencies=[Depends(per_user(autosave_limiter))])
def upsert_writing_response(
    attempt_id: UUID,
    prompt_id: UUID,
    body: WritingResponseUpsert,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> WritingResponseOut:
    """Autosaved essay draft: one row per (attempt, task), overwritten on
    every save, never duplicated. The server's word count is authoritative."""

    attempt = _require_open_attempt(session, attempt_id, user_id)
    prompt = session.get(WritingPrompt, prompt_id)
    if prompt is None or prompt.section_id not in _section_ids_for_version(session, attempt.test_version_id):
        raise HTTPException(status_code=404, detail="Writing task is not part of this attempt.")

    row = session.exec(
        select(WritingResponse).where(WritingResponse.attempt_id == attempt.id, WritingResponse.writing_prompt_id == prompt_id)
    ).first()
    if row is not None and is_stale(row.client_seq, body.client_seq):
        out = _to_writing_out(row)
        session.rollback()
        return out
    now = datetime.now(timezone.utc)
    if row is None:
        row = WritingResponse(id=uuid4(), attempt_id=attempt.id, writing_prompt_id=prompt_id, body=body.body, version=1, saved_at=now)
    else:
        row.body = body.body
        row.version += 1
        row.saved_at = now
    if body.client_seq is not None:
        row.client_seq = body.client_seq
    row.word_count = count_words(body.body)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_writing_out(row)


# --- PUT /attempts/{id}/speaking/{promptId} (FR-SPEAK-06) ----------------


@router.put("/attempts/{attempt_id}/speaking/{prompt_id}", response_model=SpeakingResponseOut, dependencies=[Depends(per_user(autosave_limiter))])
def upsert_speaking_response(
    attempt_id: UUID,
    prompt_id: UUID,
    body: SpeakingResponseUpsert,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> SpeakingResponseOut:
    """Records *metadata* about an answer - mode, duration, completion, or
    the typed answer in text mode. The recording itself never reaches the
    server (spec 3.8 recording storage policy)."""

    attempt = _require_open_attempt(session, attempt_id, user_id)
    prompt = session.get(SpeakingPrompt, prompt_id)
    if prompt is None or prompt.section_id not in _section_ids_for_version(session, attempt.test_version_id):
        raise HTTPException(status_code=404, detail="Speaking prompt is not part of this attempt.")
    if body.response_mode != "text" and body.text_response:
        raise HTTPException(status_code=422, detail="Only a text-mode answer can carry a typed response.")
    if body.response_mode != "live" and (body.transcript or body.turns):
        raise HTTPException(status_code=422, detail="Transcripts and turns belong to live-mode answers only.")

    row = session.exec(
        select(SpeakingResponse).where(SpeakingResponse.attempt_id == attempt.id, SpeakingResponse.speaking_prompt_id == prompt_id)
    ).first()
    if row is not None and is_stale(row.client_seq, body.client_seq):
        out = _to_speaking_out(row)
        session.rollback()
        return out
    now = datetime.now(timezone.utc)
    if row is None:
        row = SpeakingResponse(
            id=uuid4(), attempt_id=attempt.id, speaking_prompt_id=prompt_id, response_mode=body.response_mode, version=1, saved_at=now
        )
    else:
        row.version += 1
        row.saved_at = now
    if body.client_seq is not None:
        row.client_seq = body.client_seq
    row.response_mode = body.response_mode
    row.duration_seconds = body.duration_seconds
    row.text_response = body.text_response if body.response_mode == "text" else None
    row.completed = body.completed
    if body.response_mode == "live":
        # FR-LIVE-11: keep words only with consent; timings are always kept.
        keep = body.save_transcript
        row.transcript = body.transcript if keep else None
        row.stt_model = body.stt_model
        row.turns = (
            [turn.model_dump() | ({} if keep else {"transcript": None}) for turn in body.turns]
            if body.turns is not None
            else None
        )
    else:
        row.transcript = None
        row.stt_model = None
        row.turns = None
    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_speaking_out(row)


# --- PUT /attempts/{id}/self-review (FR-WRITE-05, FR-SPEAK-07) -----------


@router.put("/attempts/{attempt_id}/self-review", response_model=SelfReviewOut, dependencies=[Depends(per_user(autosave_limiter))])
def upsert_self_review(
    attempt_id: UUID,
    body: SelfReviewIn,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> SelfReviewOut:
    """The student's own rubric ratings. Allowed only after submission
    (a DB trigger enforces the same) and always labelled self-assessment,
    never a band score."""

    attempt = require_owned_attempt(session, attempt_id, UUID(user_id))
    attempt = reconcile_expiry(session, attempt)
    if attempt.status == "in_progress":
        raise HTTPException(status_code=409, detail="Self-review opens once you've submitted.")

    row = session.get(SelfReview, attempt.id)
    now = datetime.now(timezone.utc)
    if row is None:
        row = SelfReview(attempt_id=attempt.id, ratings=body.ratings, checklist=body.checklist, notes=body.notes, updated_at=now)
    else:
        row.ratings = body.ratings
        row.checklist = body.checklist
        row.notes = body.notes
        row.updated_at = now
    session.add(row)
    session.commit()
    session.refresh(row)
    return SelfReviewOut(ratings=row.ratings, checklist=row.checklist, notes=row.notes, updated_at=row.updated_at)


# --- GET /me/attempts (FR-RESULT-01 history) ------------------------------


def _date_bound(value: date | None, end_of_day: bool) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.max if end_of_day else time.min, tzinfo=timezone.utc)


def _response_totals(session: Session, attempt_ids: list[UUID]) -> tuple[dict[UUID, int], dict[UUID, int]]:
    """Words written per Writing attempt and completed answers per Speaking attempt."""

    words: dict[UUID, int] = {}
    answers: dict[UUID, int] = {}
    if attempt_ids:
        for row in session.exec(select(WritingResponse).where(WritingResponse.attempt_id.in_(attempt_ids))).all():
            words[row.attempt_id] = words.get(row.attempt_id, 0) + row.word_count
        for row in session.exec(select(SpeakingResponse).where(SpeakingResponse.attempt_id.in_(attempt_ids))).all():
            if row.completed:
                answers[row.attempt_id] = answers.get(row.attempt_id, 0) + 1
    return words, answers


@router.get("/me/attempts", response_model=AttemptHistoryPage)
def list_my_attempts(
    module: str | None = Query(default=None, pattern="^(listening|reading|writing|speaking)$"),
    track: str | None = Query(default=None, pattern="^(academic|general|both)$"),
    status: str | None = Query(default=None, pattern="^(in_progress|submitted|expired|cancelled)$"),
    kind: str | None = Query(default=None, pattern="^(practice|mock|full_mock)$"),
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> AttemptHistoryPage:
    """The student's own attempts, newest first, filtered and paginated.
    kind=full_mock keeps only module attempts taken inside a full mock."""

    student_id = UUID(user_id)
    expire_overdue(session, student_id)
    linked = mock_ids_by_attempt(session, student_id)

    query = (
        select(Attempt, Test)
        .join(TestVersion, TestVersion.id == Attempt.test_version_id)
        .join(Test, Test.id == TestVersion.test_id)
        .where(Attempt.student_id == student_id)
    )
    if module:
        query = query.where(Test.module == module)
    if track:
        query = query.where(Test.track == track)
    if status:
        query = query.where(Attempt.status == status)
    if kind == "full_mock":
        query = query.where(Attempt.id.in_(list(linked))) if linked else query.where(Attempt.id.is_(None))
    elif kind:
        query = query.where(Test.kind == kind)
    if date_from:
        query = query.where(Attempt.started_at >= _date_bound(date_from, False))
    if date_to:
        query = query.where(Attempt.started_at <= _date_bound(date_to, True))

    total = session.exec(select(func.count()).select_from(query.subquery())).one()
    rows = session.exec(query.order_by(Attempt.started_at.desc()).offset(offset).limit(limit)).all()
    words, answers = _response_totals(session, [attempt.id for attempt, _ in rows])

    items = [
        AttemptHistoryItem(
            id=str(attempt.id),
            test_id=str(test.id),
            test_title=test.title,
            module=test.module,
            track=test.track,
            kind=test.kind,
            status=attempt.status,
            mode=attempt.mode,
            assessment_status=attempt.assessment_status,
            started_at=attempt.started_at,
            submitted_at=attempt.submitted_at,
            raw_score=attempt.raw_score,
            max_score=attempt.max_score,
            estimated_band=attempt.estimated_band,
            words_written=words.get(attempt.id, 0) if test.module == "writing" else None,
            speaking_answers=answers.get(attempt.id, 0) if test.module == "speaking" else None,
            mock_attempt_id=str(linked[attempt.id]) if attempt.id in linked else None,
        )
        for attempt, test in rows
    ]
    return AttemptHistoryPage(items=items, total=total, limit=limit, offset=offset)


# --- GET /me/progress (FR-RESULT-06) ---------------------------------------


@router.get("/me/progress", response_model=ProgressOut)
def get_my_progress(
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> ProgressOut:
    """Everything the progress charts need, from finished attempts only:
    one point per attempt (oldest first), plus completion counts."""

    student_id = UUID(user_id)
    expire_overdue(session, student_id)
    rows = session.exec(
        select(Attempt, Test)
        .join(TestVersion, TestVersion.id == Attempt.test_version_id)
        .join(Test, Test.id == TestVersion.test_id)
        .where(Attempt.student_id == student_id, Attempt.status.in_(["submitted", "expired"]))
        .order_by(Attempt.submitted_at)
    ).all()
    words, answers = _response_totals(session, [attempt.id for attempt, _ in rows])

    points = []
    completed = {module: 0 for module in MODULE_ORDER}
    for attempt, test in rows:
        completed[test.module] = completed.get(test.module, 0) + 1
        points.append(
            ProgressPoint(
                attempt_id=str(attempt.id),
                module=test.module,
                test_title=test.title,
                kind=test.kind,
                started_at=attempt.started_at,
                submitted_at=attempt.submitted_at or attempt.started_at,
                assessment_status=attempt.assessment_status,
                raw_score=attempt.raw_score,
                max_score=attempt.max_score,
                percentage=(
                    round(attempt.raw_score / attempt.max_score * 100, 1)
                    if attempt.raw_score is not None and attempt.max_score
                    else None
                ),
                estimated_band=attempt.estimated_band,
                words_written=words.get(attempt.id, 0) if test.module == "writing" else None,
                speaking_answers=answers.get(attempt.id, 0) if test.module == "speaking" else None,
            )
        )

    mocks_completed = session.exec(
        select(func.count())
        .select_from(MockAttempt)
        .where(MockAttempt.student_id == student_id, MockAttempt.status == "completed")
    ).one()
    certificates = session.exec(
        select(func.count())
        .select_from(Certificate)
        .where(Certificate.student_id == student_id, Certificate.revoked_at.is_(None))
    ).one()
    return ProgressOut(
        completed_by_module=completed,
        points=points,
        full_mocks_completed=mocks_completed,
        certificates=certificates,
    )


# --- PUT /attempts/{id}/responses/{questionId} ---------------------------


@router.put("/attempts/{attempt_id}/responses/{question_id}", response_model=ResponseOut, dependencies=[Depends(per_user(autosave_limiter))])
def upsert_response(
    attempt_id: UUID,
    question_id: UUID,
    body: ResponseUpsert,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> ResponseOut:
    attempt = _require_open_attempt(session, attempt_id, user_id)

    question = session.get(Question, question_id)
    if question is None or question.section_id not in _section_ids_for_version(session, attempt.test_version_id):
        raise HTTPException(status_code=404, detail="Question is not part of this attempt.")

    existing = session.exec(
        select(Response).where(Response.attempt_id == attempt.id, Response.question_id == question_id)
    ).first()
    if existing is not None and is_stale(existing.client_seq, body.client_seq):
        out = _to_response_out(existing)
        session.rollback()
        return out
    now = datetime.now(timezone.utc)
    if existing is None:
        existing = Response(
            id=uuid4(),
            attempt_id=attempt.id,
            question_id=question_id,
            response=body.response,
            version=1,
            saved_at=now,
        )
    else:
        existing.response = body.response
        existing.version += 1
        existing.saved_at = now
    if body.client_seq is not None:
        existing.client_seq = body.client_seq

    session.add(existing)
    session.commit()
    session.refresh(existing)
    return _to_response_out(existing)


def _to_response_out(row: Response) -> ResponseOut:
    return ResponseOut(
        question_id=str(row.question_id), response=row.response, version=row.version, saved_at=row.saved_at, client_seq=row.client_seq
    )


# --- POST /attempts/{id}/submit ------------------------------------------


@router.post("/attempts/{attempt_id}/submit", response_model=AttemptOut, dependencies=[Depends(per_user(submit_limiter))])
def submit_attempt(
    attempt_id: UUID,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> AttemptOut:
    # Locked so a double submit (two tabs, a retry) grades exactly once.
    attempt = require_owned_attempt(session, attempt_id, UUID(user_id), lock=True)
    attempt = reconcile_expiry(session, attempt)

    # Idempotent: calling submit again after it's already terminal just
    # returns the existing result rather than erroring, same as any other
    # safe-to-retry finalize endpoint (FR-TEST-07).
    if attempt.status != "in_progress":
        return to_attempt_out(attempt)

    attempt = _finalize(session, attempt, "submitted", datetime.now(timezone.utc))
    return to_attempt_out(attempt)


# --- GET /attempts/{id}/results ------------------------------------------


@router.get("/attempts/{attempt_id}/results", response_model=ResultsOut)
def get_results(
    attempt_id: UUID,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> ResultsOut:
    attempt = require_owned_attempt(session, attempt_id, UUID(user_id))
    attempt = reconcile_expiry(session, attempt)
    if attempt.status == "in_progress":
        # Never leak correctness/answer-key data before submission.
        raise HTTPException(status_code=409, detail="Attempt has not been submitted yet.")

    questions_with_keys = _questions_with_keys(session, attempt.test_version_id)
    responses = session.exec(select(Response).where(Response.attempt_id == attempt.id)).all()
    responses_by_question = {r.question_id: r for r in responses}

    question_results = []
    for question, answer_key in questions_with_keys:
        response_row = responses_by_question.get(question.id)
        your_answer = response_row.response if response_row else None
        if answer_key is None:
            question_results.append(
                ResultQuestion(
                    question_id=str(question.id),
                    type=question.type,
                    prompt=question.prompt,
                    your_answer=your_answer,
                    correct_answers=None,
                    accepted_aliases=None,
                    is_correct=False,
                    marks_awarded=0,
                    marks_available=0,
                    rationale=None,
                )
            )
            continue
        frozen = (attempt.question_marks or {}).get(str(question.id))
        if frozen is not None:
            awarded = float(frozen)
            correct = question.marks > 0 and awarded >= question.marks
        else:
            # Attempts submitted before 0012 have no frozen marks.
            awarded = marks_for(question, answer_key, your_answer)
            correct = is_correct(your_answer, answer_key.correct_answers, answer_key.accepted_aliases, question.options, question.type)
        question_results.append(
            ResultQuestion(
                question_id=str(question.id),
                type=question.type,
                prompt=question.prompt,
                your_answer=your_answer,
                correct_answers=answer_key.correct_answers,
                accepted_aliases=answer_key.accepted_aliases,
                is_correct=correct,
                marks_awarded=awarded,
                marks_available=question.marks,
                rationale=answer_key.rationale,
            )
        )

    media_transcripts = [
        MediaTranscriptOut(media_asset_id=str(media.id), type=media.type, transcript=media.transcript)
        for media in _media_assets_for_version(session, attempt.test_version_id)
    ]

    test = _test_for_attempt(session, attempt)

    writing_rows = {
        row.writing_prompt_id: row
        for row in session.exec(select(WritingResponse).where(WritingResponse.attempt_id == attempt.id)).all()
    }
    writing_results = []
    for prompt in _writing_prompts(session, attempt.test_version_id):
        row = writing_rows.get(prompt.id)
        body_text = row.body if row else ""
        writing_results.append(
            WritingResult(
                writing_prompt_id=str(prompt.id),
                task_number=prompt.task_number,
                prompt=prompt.prompt,
                min_words=prompt.min_words,
                body=body_text,
                word_count=row.word_count if row else 0,
                # An unanswered task still gets diagnostics ("no text submitted").
                diagnostics=(row.diagnostics if row and row.diagnostics else diagnose(body_text, prompt.min_words, prompt.letter_format)),
                examiner_notes=prompt.examiner_notes,
            )
        )

    speaking_rows = {
        row.speaking_prompt_id: row
        for row in session.exec(select(SpeakingResponse).where(SpeakingResponse.attempt_id == attempt.id)).all()
    }
    speaking_results = []
    for prompt in _speaking_prompts(session, attempt.test_version_id):
        row = speaking_rows.get(prompt.id)
        speaking_results.append(
            SpeakingResult(
                speaking_prompt_id=str(prompt.id),
                part=prompt.part,
                prompt=prompt.prompt,
                bullets=prompt.bullets,
                response_mode=row.response_mode if row else None,
                duration_seconds=row.duration_seconds if row else None,
                text_response=row.text_response if row else None,
                completed=row.completed if row else False,
                transcript=row.transcript if row else None,
                stt_model=row.stt_model if row else None,
                turns=row.turns if row else None,
            )
        )

    review = session.get(SelfReview, attempt.id)
    base = to_attempt_out(attempt)
    return ResultsOut(
        **base.model_dump(),
        module=test.module,
        questions=question_results,
        media_transcripts=media_transcripts,
        writing=writing_results,
        speaking=speaking_results,
        self_review=(
            SelfReviewOut(ratings=review.ratings, checklist=review.checklist, notes=review.notes, updated_at=review.updated_at)
            if review
            else None
        ),
    )

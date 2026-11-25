"""Review my mistakes: the Listening and Reading questions a student
lost marks on, gathered from their finished attempts, to practise again.

A question counts as a mistake when the newest finished attempt that
contained it scored it below full marks, so getting it right in a later
attempt of the same test takes it off the list. Marks come from the
attempt's frozen `question_marks` (migration 0012), or are re-marked for
attempts finished before that.

The student has already seen these answers on their results page, so
showing them again leaks nothing new. Checking a retry happens on the
server with the same grader as real tests.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ...core.security import get_current_user_id
from ...db.session import get_session
from ...models import AnswerKey, Attempt, Question, Response, Section, Test, TestVersion
from ...services.grading import credit, marks_for
from ...services.rate_limit import autosave_limiter, per_user
from .attempts import _questions_with_keys, expire_overdue

router = APIRouter()

FINISHED = ("submitted", "expired")
# Enough history for a useful list without scanning years of attempts.
MAX_ATTEMPTS_SCANNED = 100


class MistakeOut(BaseModel):
    question_id: str
    type: str
    prompt: str
    options: Any | None
    marks: float
    marks_awarded: float
    your_answer: Any | None
    correct_answers: Any
    accepted_aliases: Any | None
    rationale: str | None
    instructions: str | None
    module: Literal["listening", "reading"]
    test_title: str
    attempt_id: str
    submitted_at: datetime | None


class MistakesPage(BaseModel):
    items: list[MistakeOut]
    total: int
    by_module: dict[str, int]


class RetryIn(BaseModel):
    response: Any = Field(default=None)


class RetryOut(BaseModel):
    correct: bool
    credit: float
    correct_answers: Any
    accepted_aliases: Any | None
    rationale: str | None


def _mistakes(session: Session, student_id: UUID) -> list[MistakeOut]:
    expire_overdue(session, student_id)
    rows = session.exec(
        select(Attempt, Test)
        .join(TestVersion, TestVersion.id == Attempt.test_version_id)
        .join(Test, Test.id == TestVersion.test_id)
        .where(
            Attempt.student_id == student_id,
            Attempt.status.in_(FINISHED),
            Attempt.assessment_status == "objective",
        )
        .order_by(Attempt.submitted_at.desc())
        .limit(MAX_ATTEMPTS_SCANNED)
    ).all()

    seen: set[UUID] = set()
    found: list[MistakeOut] = []
    instructions_by_section: dict[UUID, str | None] = {}
    for attempt, test in rows:
        questions = _questions_with_keys(session, attempt.test_version_id)
        if not questions:
            continue
        responses = {
            r.question_id: r.response for r in session.exec(select(Response).where(Response.attempt_id == attempt.id)).all()
        }
        frozen = attempt.question_marks or {}
        for question, key in sorted(questions, key=lambda pair: pair[0].display_order):
            if key is None or question.id in seen:
                continue
            seen.add(question.id)  # the newest attempt decides
            answer = responses.get(question.id)
            awarded = float(frozen[str(question.id)]) if str(question.id) in frozen else marks_for(question, key, answer)
            if awarded >= question.marks:
                continue
            if question.section_id not in instructions_by_section:
                section = session.get(Section, question.section_id)
                instructions_by_section[question.section_id] = section.instructions if section else None
            found.append(
                MistakeOut(
                    question_id=str(question.id),
                    type=question.type,
                    prompt=question.prompt,
                    options=question.options,
                    marks=question.marks,
                    marks_awarded=awarded,
                    your_answer=answer,
                    correct_answers=key.correct_answers,
                    accepted_aliases=key.accepted_aliases,
                    rationale=key.rationale,
                    instructions=instructions_by_section[question.section_id],
                    module=test.module,
                    test_title=test.title,
                    attempt_id=str(attempt.id),
                    submitted_at=attempt.submitted_at,
                )
            )
    return found


@router.get("/me/mistakes", response_model=MistakesPage)
def list_my_mistakes(
    module: str | None = Query(default=None, pattern="^(listening|reading)$"),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> MistakesPage:
    everything = _mistakes(session, UUID(user_id))
    by_module = {"listening": 0, "reading": 0}
    for item in everything:
        by_module[item.module] += 1
    chosen = [item for item in everything if module is None or item.module == module]
    return MistakesPage(items=chosen[offset : offset + limit], total=len(chosen), by_module=by_module)


@router.post(
    "/me/mistakes/{question_id}/check",
    response_model=RetryOut,
    dependencies=[Depends(per_user(autosave_limiter))],
)
def check_retry(
    question_id: UUID,
    body: RetryIn,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> RetryOut:
    """Marks a retry of a question. Only for questions from the student's
    own finished attempts, whose answers they have already been shown, so
    this can't be used to fish for the keys of a test they haven't taken."""

    question = session.get(Question, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found.")
    section = session.get(Section, question.section_id)
    taken = (
        section is not None
        and session.exec(
            select(Attempt.id).where(
                Attempt.student_id == UUID(user_id),
                Attempt.test_version_id == section.test_version_id,
                Attempt.status.in_(FINISHED),
            )
        ).first()
        is not None
    )
    key = session.exec(select(AnswerKey).where(AnswerKey.question_id == question_id)).first()
    if not taken or key is None:
        raise HTTPException(status_code=404, detail="Question not found.")
    share = credit(body.response, key.correct_answers, key.accepted_aliases, question.options, question.type)
    return RetryOut(
        correct=share >= 1.0,
        credit=round(share, 2),
        correct_answers=key.correct_answers,
        accepted_aliases=key.accepted_aliases,
        rationale=key.rationale,
    )

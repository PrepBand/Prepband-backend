"""Phase 5: the live AI practice examiner's fixed lines (examiner_phrases).

Students read them to run the examiner; admins edit the wording and attach
pre-generated voice clips. Keys are fixed by the migration (the examiner
script engine in the frontend looks them up by key), so admins can change
what a line says but not add or remove lines.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ...core.roles import require_admin
from ...core.security import get_current_user_id
from ...db.session import get_session
from ...models import ExaminerPhrase

router = APIRouter()


class ExaminerPhraseOut(BaseModel):
    key: str
    text: str
    audio_url: str | None
    updated_at: datetime


class ExaminerPhraseUpdate(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    # Explicit null removes a clip (e.g. after the wording changed).
    audio_url: str | None = Field(default=None, pattern=r"^https://", max_length=1000)


def _out(row: ExaminerPhrase) -> ExaminerPhraseOut:
    return ExaminerPhraseOut(key=row.key, text=row.text, audio_url=row.audio_url, updated_at=row.updated_at)


def _all(session: Session) -> list[ExaminerPhraseOut]:
    return [_out(row) for row in session.exec(select(ExaminerPhrase).order_by(ExaminerPhrase.key)).all()]


@router.get("/examiner/phrases", response_model=list[ExaminerPhraseOut])
def list_phrases(
    _: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> list[ExaminerPhraseOut]:
    return _all(session)


@router.get("/admin/examiner/phrases", response_model=list[ExaminerPhraseOut])
def admin_list_phrases(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[ExaminerPhraseOut]:
    return _all(session)


@router.put("/admin/examiner/phrases/{key}", response_model=ExaminerPhraseOut)
def admin_update_phrase(
    key: str,
    body: ExaminerPhraseUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ExaminerPhraseOut:
    row = session.get(ExaminerPhrase, key)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown examiner line.")
    text = body.text.strip()
    # A clip recorded for the old wording would no longer match: drop it
    # unless the request supplies one for the new text.
    if text != row.text and body.audio_url == row.audio_url:
        row.audio_url = None
    else:
        row.audio_url = body.audio_url
    row.text = text
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _out(row)

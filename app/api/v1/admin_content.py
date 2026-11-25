"""Admin CRUD for the content that lives *under* a test version - sections,
passages, media assets, questions and answer keys (FR-ADMIN-02). Tests
themselves (create/list/edit-metadata/publish) and the user overview live
in admin.py; this file is only the nested-resource authoring surface that
closes the gap identified after Step 1's first pass - without it, a test
could never actually have a section, so publish_test could never succeed.

Every write here is rejected with 409 once the parent test_version is
published - the same "a published version is frozen" rule the DB trigger
(prevent_published_version_mutation) enforces for test_versions directly,
mirrored here for its children since the trigger only guards the
test_versions row itself, not sections/passages/media_assets/questions.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ...core.roles import require_admin
from ...db.session import get_session
from ...models import AnswerKey, MediaAsset, Passage, Question, Section, SpeakingPrompt, Test, TestVersion, WritingPrompt
from ...schemas.content import (
    AnswerKeyOut,
    AnswerKeyUpsert,
    MediaAssetCreate,
    MediaAssetOut,
    MediaAssetUpdate,
    PassageCreate,
    PassageOut,
    PassageUpdate,
    QuestionCreate,
    QuestionOut,
    QuestionUpdate,
    SectionCreate,
    SectionOut,
    SectionUpdate,
    SpeakingPromptCreate,
    SpeakingPromptOut,
    SpeakingPromptUpdate,
    WritingPromptCreate,
    WritingPromptOut,
    WritingPromptUpdate,
)

router = APIRouter()


# --- shared lookups / draft-only guards --------------------------------


def _get_test_or_404(session: Session, test_id: UUID) -> Test:
    test = session.get(Test, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Test not found.")
    return test


def lock_test_version(session: Session, version_id: UUID) -> TestVersion | None:
    """Reads a test version with a row lock held until the transaction ends.

    Every content write and publish_test take this lock first, so a write
    that has seen a draft finishes before a publish can freeze the version,
    and a publish that has frozen it makes later writes see "published"
    (the database trigger only guards the test_versions row itself)."""

    return session.exec(
        select(TestVersion).where(TestVersion.id == version_id).with_for_update().execution_options(populate_existing=True)
    ).first()


def _get_draft_version_for_test(session: Session, test: Test) -> TestVersion:
    if test.current_version_id is None:
        raise HTTPException(status_code=409, detail="Test has no version to add content to.")
    version = lock_test_version(session, test.current_version_id)
    if version is None or version.status != "draft":
        raise HTTPException(status_code=409, detail="Only a draft version can be edited.")
    return version


def _get_section_or_404(session: Session, section_id: UUID) -> Section:
    section = session.get(Section, section_id)
    if section is None:
        raise HTTPException(status_code=404, detail="Section not found.")
    return section


def _require_section_version_draft(session: Session, section: Section) -> None:
    version = lock_test_version(session, section.test_version_id)
    if version is None or version.status != "draft":
        raise HTTPException(status_code=409, detail="Only a draft version can be edited.")


def _get_passage_or_404(session: Session, passage_id: UUID) -> Passage:
    passage = session.get(Passage, passage_id)
    if passage is None:
        raise HTTPException(status_code=404, detail="Passage not found.")
    return passage


def _get_media_asset_or_404(session: Session, media_asset_id: UUID) -> MediaAsset:
    media_asset = session.get(MediaAsset, media_asset_id)
    if media_asset is None:
        raise HTTPException(status_code=404, detail="Media asset not found.")
    return media_asset


def _get_question_or_404(session: Session, question_id: UUID) -> Question:
    question = session.get(Question, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found.")
    return question


# --- output helpers ------------------------------------------------------


def _to_section_out(section: Section) -> SectionOut:
    data = section.model_dump()
    data["id"] = str(data["id"])
    data["test_version_id"] = str(data["test_version_id"])
    return SectionOut(**data)


def _to_passage_out(passage: Passage) -> PassageOut:
    data = passage.model_dump()
    data["id"] = str(data["id"])
    data["section_id"] = str(data["section_id"])
    return PassageOut(**data)


def _to_media_asset_out(media_asset: MediaAsset) -> MediaAssetOut:
    data = media_asset.model_dump()
    data["id"] = str(data["id"])
    data["section_id"] = str(data["section_id"]) if data["section_id"] else None
    return MediaAssetOut(**data)


def _to_question_out(question: Question) -> QuestionOut:
    data = question.model_dump()
    data["id"] = str(data["id"])
    data["section_id"] = str(data["section_id"])
    return QuestionOut(**data)


def _to_answer_key_out(answer_key: AnswerKey) -> AnswerKeyOut:
    data = answer_key.model_dump()
    data["id"] = str(data["id"])
    data["question_id"] = str(data["question_id"])
    return AnswerKeyOut(**data)


# --- sections --------------------------------------------------------


@router.get("/admin/tests/{test_id}/sections", response_model=list[SectionOut])
def list_sections(
    test_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[SectionOut]:
    test = _get_test_or_404(session, test_id)
    if test.current_version_id is None:
        return []
    sections = session.exec(
        select(Section)
        .where(Section.test_version_id == test.current_version_id)
        .order_by(Section.order_index)
    ).all()
    return [_to_section_out(section) for section in sections]


@router.post("/admin/tests/{test_id}/sections", response_model=SectionOut, status_code=201)
def create_section(
    test_id: UUID,
    body: SectionCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> SectionOut:
    test = _get_test_or_404(session, test_id)
    version = _get_draft_version_for_test(session, test)

    section = Section(
        id=uuid4(),
        test_version_id=version.id,
        module=body.module,
        order_index=body.order_index,
        instructions=body.instructions,
        timing_seconds=body.timing_seconds,
        created_at=datetime.now(timezone.utc),
    )
    session.add(section)
    session.commit()
    session.refresh(section)
    return _to_section_out(section)


@router.patch("/admin/sections/{section_id}", response_model=SectionOut)
def update_section(
    section_id: UUID,
    body: SectionUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> SectionOut:
    section = _get_section_or_404(session, section_id)
    _require_section_version_draft(session, section)

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(section, field, value)
    session.add(section)
    session.commit()
    session.refresh(section)
    return _to_section_out(section)


@router.delete("/admin/sections/{section_id}", status_code=204)
def delete_section(
    section_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    section = _get_section_or_404(session, section_id)
    _require_section_version_draft(session, section)
    session.delete(section)
    session.commit()


# --- passages --------------------------------------------------------


@router.post("/admin/sections/{section_id}/passages", response_model=PassageOut, status_code=201)
def create_passage(
    section_id: UUID,
    body: PassageCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PassageOut:
    section = _get_section_or_404(session, section_id)
    _require_section_version_draft(session, section)

    passage = Passage(
        id=uuid4(),
        section_id=section.id,
        title=body.title,
        body=body.body,
        source=body.source,
        license=body.license,
        attribution=body.attribution,
        created_at=datetime.now(timezone.utc),
    )
    session.add(passage)
    session.commit()
    session.refresh(passage)
    return _to_passage_out(passage)


@router.patch("/admin/passages/{passage_id}", response_model=PassageOut)
def update_passage(
    passage_id: UUID,
    body: PassageUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PassageOut:
    passage = _get_passage_or_404(session, passage_id)
    section = _get_section_or_404(session, passage.section_id)
    _require_section_version_draft(session, section)

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(passage, field, value)
    session.add(passage)
    session.commit()
    session.refresh(passage)
    return _to_passage_out(passage)


@router.delete("/admin/passages/{passage_id}", status_code=204)
def delete_passage(
    passage_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    passage = _get_passage_or_404(session, passage_id)
    section = _get_section_or_404(session, passage.section_id)
    _require_section_version_draft(session, section)
    session.delete(passage)
    session.commit()


# --- media assets ------------------------------------------------------


@router.post("/admin/sections/{section_id}/media-assets", response_model=MediaAssetOut, status_code=201)
def create_media_asset(
    section_id: UUID,
    body: MediaAssetCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> MediaAssetOut:
    section = _get_section_or_404(session, section_id)
    _require_section_version_draft(session, section)

    media_asset = MediaAsset(
        id=uuid4(),
        section_id=section.id,
        type=body.type,
        storage_key=body.storage_key,
        public_url=body.public_url,
        content_type=body.content_type,
        duration_seconds=body.duration_seconds,
        size_bytes=body.size_bytes,
        attribution=body.attribution,
        license=body.license,
        transcript=body.transcript,
        created_at=datetime.now(timezone.utc),
    )
    session.add(media_asset)
    session.commit()
    session.refresh(media_asset)
    return _to_media_asset_out(media_asset)


@router.patch("/admin/media-assets/{media_asset_id}", response_model=MediaAssetOut)
def update_media_asset(
    media_asset_id: UUID,
    body: MediaAssetUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> MediaAssetOut:
    media_asset = _get_media_asset_or_404(session, media_asset_id)
    if media_asset.section_id is not None:
        section = _get_section_or_404(session, media_asset.section_id)
        _require_section_version_draft(session, section)

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(media_asset, field, value)
    session.add(media_asset)
    session.commit()
    session.refresh(media_asset)
    return _to_media_asset_out(media_asset)


@router.delete("/admin/media-assets/{media_asset_id}", status_code=204)
def delete_media_asset(
    media_asset_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    media_asset = _get_media_asset_or_404(session, media_asset_id)
    if media_asset.section_id is not None:
        section = _get_section_or_404(session, media_asset.section_id)
        _require_section_version_draft(session, section)
    session.delete(media_asset)
    session.commit()


# --- questions ---------------------------------------------------------


@router.get("/admin/sections/{section_id}/questions", response_model=list[QuestionOut])
def list_questions(
    section_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[QuestionOut]:
    _get_section_or_404(session, section_id)
    questions = session.exec(
        select(Question).where(Question.section_id == section_id).order_by(Question.display_order)
    ).all()
    return [_to_question_out(question) for question in questions]


@router.post("/admin/sections/{section_id}/questions", response_model=QuestionOut, status_code=201)
def create_question(
    section_id: UUID,
    body: QuestionCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> QuestionOut:
    section = _get_section_or_404(session, section_id)
    _require_section_version_draft(session, section)

    question = Question(
        id=uuid4(),
        section_id=section.id,
        type=body.type,
        prompt=body.prompt,
        options=body.options,
        display_order=body.display_order,
        marks=body.marks,
        created_at=datetime.now(timezone.utc),
    )
    session.add(question)
    session.commit()
    session.refresh(question)
    return _to_question_out(question)


@router.patch("/admin/questions/{question_id}", response_model=QuestionOut)
def update_question(
    question_id: UUID,
    body: QuestionUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> QuestionOut:
    question = _get_question_or_404(session, question_id)
    section = _get_section_or_404(session, question.section_id)
    _require_section_version_draft(session, section)

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(question, field, value)
    session.add(question)
    session.commit()
    session.refresh(question)
    return _to_question_out(question)


@router.delete("/admin/questions/{question_id}", status_code=204)
def delete_question(
    question_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    question = _get_question_or_404(session, question_id)
    section = _get_section_or_404(session, question.section_id)
    _require_section_version_draft(session, section)
    # answer_keys.question_id has `on delete cascade`, so this also removes
    # the question's answer key, if any - no separate cleanup needed.
    session.delete(question)
    session.commit()


# --- answer keys (1:1 with a question) ----------------------------------


@router.get("/admin/questions/{question_id}/answer-key", response_model=AnswerKeyOut)
def get_answer_key(
    question_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AnswerKeyOut:
    _get_question_or_404(session, question_id)
    answer_key = session.exec(select(AnswerKey).where(AnswerKey.question_id == question_id)).first()
    if answer_key is None:
        raise HTTPException(status_code=404, detail="Answer key not set for this question.")
    return _to_answer_key_out(answer_key)


@router.put("/admin/questions/{question_id}/answer-key", response_model=AnswerKeyOut)
def upsert_answer_key(
    question_id: UUID,
    body: AnswerKeyUpsert,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AnswerKeyOut:
    question = _get_question_or_404(session, question_id)
    section = _get_section_or_404(session, question.section_id)
    _require_section_version_draft(session, section)

    answer_key = session.exec(select(AnswerKey).where(AnswerKey.question_id == question_id)).first()
    if answer_key is None:
        answer_key = AnswerKey(
            id=uuid4(),
            question_id=question_id,
            correct_answers=body.correct_answers,
            accepted_aliases=body.accepted_aliases,
            rationale=body.rationale,
            created_at=datetime.now(timezone.utc),
        )
    else:
        answer_key.correct_answers = body.correct_answers
        answer_key.accepted_aliases = body.accepted_aliases
        answer_key.rationale = body.rationale

    session.add(answer_key)
    session.commit()
    session.refresh(answer_key)
    return _to_answer_key_out(answer_key)


# --- writing prompts (Phase 3) -----------------------------------------


def _to_writing_prompt_out(prompt: WritingPrompt) -> WritingPromptOut:
    data = prompt.model_dump()
    data["id"] = str(data["id"])
    data["section_id"] = str(data["section_id"])
    return WritingPromptOut(**data)


def _to_speaking_prompt_out(prompt: SpeakingPrompt) -> SpeakingPromptOut:
    data = prompt.model_dump()
    data["id"] = str(data["id"])
    data["section_id"] = str(data["section_id"])
    return SpeakingPromptOut(**data)


def _check_visual(visual: dict | None) -> None:
    if visual is None:
        return
    columns, rows = visual.get("columns"), visual.get("rows")
    if not isinstance(columns, list) or not columns or not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=422, detail="A data table needs at least one column and one row.")
    if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
        raise HTTPException(status_code=422, detail="Every table row needs one value per column.")


@router.post("/admin/sections/{section_id}/writing-prompts", response_model=WritingPromptOut, status_code=201)
def create_writing_prompt(
    section_id: UUID,
    body: WritingPromptCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> WritingPromptOut:
    section = _get_section_or_404(session, section_id)
    _require_section_version_draft(session, section)
    _check_visual(body.visual)
    prompt = WritingPrompt(id=uuid4(), section_id=section.id, created_at=datetime.now(timezone.utc), **body.model_dump())
    session.add(prompt)
    session.commit()
    session.refresh(prompt)
    return _to_writing_prompt_out(prompt)


@router.patch("/admin/writing-prompts/{prompt_id}", response_model=WritingPromptOut)
def update_writing_prompt(
    prompt_id: UUID,
    body: WritingPromptUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> WritingPromptOut:
    prompt = session.get(WritingPrompt, prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail="Writing task not found.")
    _require_section_version_draft(session, _get_section_or_404(session, prompt.section_id))
    updates = body.model_dump(exclude_unset=True)
    if "visual" in updates:
        _check_visual(updates["visual"])
    for field, value in updates.items():
        setattr(prompt, field, value)
    session.add(prompt)
    session.commit()
    session.refresh(prompt)
    return _to_writing_prompt_out(prompt)


@router.delete("/admin/writing-prompts/{prompt_id}", status_code=204)
def delete_writing_prompt(
    prompt_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    prompt = session.get(WritingPrompt, prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail="Writing task not found.")
    _require_section_version_draft(session, _get_section_or_404(session, prompt.section_id))
    session.delete(prompt)
    session.commit()


# --- speaking prompts (Phase 3) ----------------------------------------


@router.post("/admin/sections/{section_id}/speaking-prompts", response_model=SpeakingPromptOut, status_code=201)
def create_speaking_prompt(
    section_id: UUID,
    body: SpeakingPromptCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> SpeakingPromptOut:
    section = _get_section_or_404(session, section_id)
    _require_section_version_draft(session, section)
    prompt = SpeakingPrompt(id=uuid4(), section_id=section.id, created_at=datetime.now(timezone.utc), **body.model_dump())
    session.add(prompt)
    session.commit()
    session.refresh(prompt)
    return _to_speaking_prompt_out(prompt)


@router.patch("/admin/speaking-prompts/{prompt_id}", response_model=SpeakingPromptOut)
def update_speaking_prompt(
    prompt_id: UUID,
    body: SpeakingPromptUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> SpeakingPromptOut:
    prompt = session.get(SpeakingPrompt, prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail="Speaking prompt not found.")
    _require_section_version_draft(session, _get_section_or_404(session, prompt.section_id))
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(prompt, field, value)
    session.add(prompt)
    session.commit()
    session.refresh(prompt)
    return _to_speaking_prompt_out(prompt)


@router.delete("/admin/speaking-prompts/{prompt_id}", status_code=204)
def delete_speaking_prompt(
    prompt_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    prompt = session.get(SpeakingPrompt, prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail="Speaking prompt not found.")
    _require_section_version_draft(session, _get_section_or_404(session, prompt.section_id))
    session.delete(prompt)
    session.commit()

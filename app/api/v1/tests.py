"""FR-TEST-01: the published test catalog. Never returns a draft test, and
never returns answer_keys - only tests.py's own admin counterpart
(admin.py / admin_content.py) sees unpublished or answer content."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from ...core.security import get_current_user_id
from ...db.session import get_session
from ...models import MediaAsset, Passage, Question, Section, SpeakingPrompt, Test, WritingPrompt
from ...schemas.attempts import (
    MediaAssetPublic,
    PassagePublic,
    QuestionPublic,
    SectionPublic,
    SpeakingPromptPublic,
    TestCatalogItem,
    TestDetail,
    WritingPromptPublic,
)

router = APIRouter()


def _to_catalog_item(test: Test) -> TestCatalogItem:
    return TestCatalogItem(
        id=str(test.id),
        title=test.title,
        module=test.module,
        track=test.track,
        kind=test.kind,
        difficulty=test.difficulty,
        duration_minutes=test.duration_minutes,
    )


@router.get("/tests", response_model=list[TestCatalogItem])
def list_tests(
    module: str | None = Query(default=None),
    track: str | None = Query(default=None),
    difficulty: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    _: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> list[TestCatalogItem]:
    query = select(Test).where(Test.status == "published")
    if module:
        query = query.where(Test.module == module)
    if track:
        query = query.where(Test.track == track)
    if difficulty:
        query = query.where(Test.difficulty == difficulty)
    if kind:
        query = query.where(Test.kind == kind)
    tests = session.exec(query.order_by(Test.created_at.desc())).all()
    return [_to_catalog_item(test) for test in tests]


@router.get("/tests/{test_id}", response_model=TestDetail)
def get_test(
    test_id: UUID,
    _: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> TestDetail:
    test = session.get(Test, test_id)
    if test is None or test.status != "published" or test.current_version_id is None:
        raise HTTPException(status_code=404, detail="Test not found.")
    return build_test_detail(session, test, test.current_version_id)


def build_test_detail(session: Session, test: Test, version_id: UUID) -> TestDetail:
    """The student-safe content of one specific test_version (no answer
    keys, transcripts or examiner notes). Attempts use this with the
    version they were started against, so a later republish never changes
    what a student sees mid-attempt."""

    sections = session.exec(
        select(Section).where(Section.test_version_id == version_id).order_by(Section.order_index)
    ).all()
    section_ids = [section.id for section in sections]

    passages_by_section: dict[UUID, list[Passage]] = {}
    media_by_section: dict[UUID, list[MediaAsset]] = {}
    questions_by_section: dict[UUID, list[Question]] = {}
    writing_by_section: dict[UUID, list[WritingPrompt]] = {}
    speaking_by_section: dict[UUID, list[SpeakingPrompt]] = {}
    if section_ids:
        for prompt in session.exec(
            select(WritingPrompt).where(WritingPrompt.section_id.in_(section_ids)).order_by(WritingPrompt.display_order)
        ).all():
            writing_by_section.setdefault(prompt.section_id, []).append(prompt)
        for prompt in session.exec(
            select(SpeakingPrompt).where(SpeakingPrompt.section_id.in_(section_ids)).order_by(SpeakingPrompt.display_order)
        ).all():
            speaking_by_section.setdefault(prompt.section_id, []).append(prompt)
        for passage in session.exec(select(Passage).where(Passage.section_id.in_(section_ids))).all():
            passages_by_section.setdefault(passage.section_id, []).append(passage)
        for media in session.exec(select(MediaAsset).where(MediaAsset.section_id.in_(section_ids))).all():
            media_by_section.setdefault(media.section_id, []).append(media)
        for question in session.exec(
            select(Question).where(Question.section_id.in_(section_ids)).order_by(Question.display_order)
        ).all():
            questions_by_section.setdefault(question.section_id, []).append(question)

    section_payloads = [
        SectionPublic(
            id=str(section.id),
            module=section.module,
            order_index=section.order_index,
            instructions=section.instructions,
            timing_seconds=section.timing_seconds,
            passages=[
                PassagePublic(
                    id=str(p.id), title=p.title, body=p.body, source=p.source, license=p.license, attribution=p.attribution
                )
                for p in passages_by_section.get(section.id, [])
            ],
            media_assets=[
                MediaAssetPublic(
                    id=str(m.id),
                    type=m.type,
                    public_url=m.public_url,
                    content_type=m.content_type,
                    duration_seconds=m.duration_seconds,
                    attribution=m.attribution,
                    license=m.license,
                )
                for m in media_by_section.get(section.id, [])
            ],
            questions=[
                QuestionPublic(
                    id=str(q.id), type=q.type, prompt=q.prompt, options=q.options, display_order=q.display_order, marks=q.marks
                )
                for q in questions_by_section.get(section.id, [])
            ],
            writing_prompts=[
                WritingPromptPublic(
                    id=str(w.id),
                    task_number=w.task_number,
                    prompt=w.prompt,
                    instructions=w.instructions,
                    min_words=w.min_words,
                    recommended_minutes=w.recommended_minutes,
                    visual=w.visual,
                    letter_format=w.letter_format,
                    display_order=w.display_order,
                )
                for w in writing_by_section.get(section.id, [])
            ],
            speaking_prompts=[
                SpeakingPromptPublic(
                    id=str(sp.id),
                    part=sp.part,
                    prompt=sp.prompt,
                    bullets=sp.bullets,
                    prep_seconds=sp.prep_seconds,
                    speak_seconds=sp.speak_seconds,
                    display_order=sp.display_order,
                    follow_ups=sp.follow_ups or [],
                    audio_url=sp.audio_url,
                )
                for sp in speaking_by_section.get(section.id, [])
            ],
        )
        for section in sections
    ]

    return TestDetail(
        id=str(test.id),
        title=test.title,
        module=test.module,
        track=test.track,
        kind=test.kind,
        difficulty=test.difficulty,
        duration_minutes=test.duration_minutes,
        sections=section_payloads,
    )

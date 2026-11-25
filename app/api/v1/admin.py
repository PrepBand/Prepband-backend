from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlmodel import Session, select

from ...core.roles import require_admin
from ...core.security import get_current_user_id
from ...db.session import get_session
from ...models import Attempt, Profile, Test, TestVersion, UserRole
from ...schemas.content import (
    AdminOverviewOut,
    AdminTestContentOut,
    AdminUserOut,
    AnswerKeyOut,
    MediaAssetOut,
    PassageOut,
    QuestionOut,
    SectionContentOut,
    SectionOut,
    SpeakingPromptOut,
    TestCreate,
    TestOut,
    TestUpdate,
    WritingPromptOut,
)
from ...services.audit import audit_note
from ...services.content_io import TestDoc, export_test, import_test, load_tree, validate_for_publish
from ...services.rate_limit import admin_bulk_limiter, per_user
from .admin_content import lock_test_version

router = APIRouter()


@router.get("/admin/ping")
def admin_ping(role: str = Depends(require_admin)) -> dict:
    """Proof that admin/student separation actually works end-to-end.

    The real admin CMS (content CRUD, user overview, etc. - FR-ADMIN-*) is
    not built yet; this route only exists to demonstrate and exercise the
    require_admin gate so Phase 1's "student/admin separation proven" exit
    criterion has something real to point at.
    """

    return {"status": "ok", "role": role}


def _to_test_out(test: Test) -> TestOut:
    data = test.model_dump()
    data["id"] = str(data["id"])
    data["current_version_id"] = str(data["current_version_id"]) if data["current_version_id"] else None
    return TestOut(**data)


@router.get("/admin/tests", response_model=list[TestOut])
def list_tests(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[TestOut]:
    tests = session.exec(select(Test).order_by(Test.created_at.desc())).all()
    return [_to_test_out(test) for test in tests]


@router.post("/admin/tests", response_model=TestOut, status_code=201)
def create_test(
    body: TestCreate,
    request: Request,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TestOut:
    """Creates a draft test and its first draft version in one call.

    A test is never content-less: every test has at least one
    test_versions row from the moment it's created, since attempts and
    future admin-authoring steps (sections/questions) always hang off a
    version, not off the test row directly.
    """

    now = datetime.now(timezone.utc)
    test = Test(
        id=uuid4(),
        title=body.title,
        module=body.module,
        track=body.track,
        kind=body.kind,
        difficulty=body.difficulty,
        duration_minutes=body.duration_minutes,
        status="draft",
        created_at=now,
        updated_at=now,
    )
    # tests.current_version_id and test_versions.test_id FK each other, and
    # there's no declared SQLModel Relationship() for SQLAlchemy to infer
    # an insert order from - without an explicit flush between them, the
    # unit of work can (and, confirmed live, does) try to insert `test`
    # with a current_version_id pointing at a test_versions row that
    # doesn't exist yet, violating tests_current_version_id_fkey.
    session.add(test)
    session.flush()

    version = TestVersion(
        id=uuid4(),
        test_id=test.id,
        version_number=1,
        status="draft",
        created_at=now,
    )
    session.add(version)
    session.flush()

    test.current_version_id = version.id
    session.add(test)
    session.commit()
    session.refresh(test)
    audit_note(request, created_test_id=test.id, title=test.title)
    return _to_test_out(test)


@router.patch("/admin/tests/{test_id}", response_model=TestOut)
def update_test(
    test_id: UUID,
    body: TestUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TestOut:
    test = session.get(Test, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Test not found.")
    if test.status != "draft":
        # FR-ADMIN-07: a published test's content is frozen; metadata edits
        # belong to a new draft version, not a live edit of what attempts
        # already point at.
        raise HTTPException(status_code=409, detail="Only a draft test can be edited.")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(test, field, value)
    session.add(test)
    session.commit()
    session.refresh(test)
    return _to_test_out(test)


@router.post("/admin/tests/{test_id}/publish", response_model=TestOut)
def publish_test(
    test_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TestOut:
    test = _get_test_or_404(session, test_id)
    # Lock the version first (the same lock every content write takes), so
    # no edit can land between the checks below and the freeze.
    locked = lock_test_version(session, test.current_version_id) if test.current_version_id else None
    session.refresh(test)
    if test.status != "draft" or (locked is not None and locked.status != "draft"):
        raise HTTPException(status_code=409, detail="Only a draft test can be published.")

    # FR-ADMIN-03 publish validation: every section has questions, every
    # question has a consistent answer key, Reading has a passage and
    # Listening has uploaded audio. A published version is immutable, so
    # anything broken here would stay broken.
    problems = validate_for_publish(session, test)
    if problems:
        raise HTTPException(status_code=422, detail={"message": "This test isn't ready to publish.", "problems": problems})

    version = session.get(TestVersion, test.current_version_id)
    if version is None:
        raise HTTPException(status_code=409, detail="Test has no version to publish.")

    now = datetime.now(timezone.utc)
    version.status = "published"
    version.published_at = now
    test.status = "published"

    session.add(version)
    session.add(test)
    session.commit()
    session.refresh(test)
    return _to_test_out(test)


def _get_test_or_404(session: Session, test_id: UUID) -> Test:
    test = session.get(Test, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Test not found.")
    return test


def _dump(model: Any) -> dict:
    data = model.model_dump()
    for key, value in data.items():
        if isinstance(value, UUID):
            data[key] = str(value)
    return data


@router.get("/admin/overview", response_model=AdminOverviewOut)
def admin_overview(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AdminOverviewOut:
    tests_by_status = dict(session.exec(select(Test.status, func.count()).group_by(Test.status)).all())
    published_by_module = dict(
        session.exec(select(Test.module, func.count()).where(Test.status == "published").group_by(Test.module)).all()
    )
    attempts_by_status = dict(session.exec(select(Attempt.status, func.count()).group_by(Attempt.status)).all())
    roles = dict(session.exec(select(UserRole.role, func.count()).group_by(UserRole.role)).all())
    total_users = session.exec(select(func.count()).select_from(Profile)).one()
    return AdminOverviewOut(
        total_users=total_users,
        admins=roles.get("admin", 0) + roles.get("super_admin", 0),
        tests_by_status=tests_by_status,
        published_by_module=published_by_module,
        attempts_by_status=attempts_by_status,
    )


# Registered before /admin/tests/{test_id} so "import" is never parsed as an id.
@router.post("/admin/tests/import", response_model=TestOut, status_code=201, dependencies=[Depends(per_user(admin_bulk_limiter))])
def import_test_json(
    body: TestDoc,
    request: Request,
    _: str = Depends(require_admin),
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> TestOut:
    """FR-ADMIN-08: creates a new draft test from a prepband-test/v1
    document (the same shape as export and database/seeds/*.json). Audio
    without a storage_key becomes a pending placeholder to upload later."""

    test = import_test(session, body, created_by=UUID(user_id))
    session.commit()
    session.refresh(test)
    audit_note(request, created_test_id=test.id, title=test.title)
    return _to_test_out(test)


@router.get("/admin/tests/{test_id}", response_model=TestOut)
def get_test(
    test_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TestOut:
    return _to_test_out(_get_test_or_404(session, test_id))


@router.get("/admin/tests/{test_id}/content", response_model=AdminTestContentOut)
def get_test_content(
    test_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AdminTestContentOut:
    """The whole editable tree in one call, answer keys and transcripts
    included. Admin-only by design: the student-facing GET /tests/{id}
    never includes either."""

    test = _get_test_or_404(session, test_id)
    tree = load_tree(session, test.current_version_id) if test.current_version_id else []
    sections = [
        SectionContentOut(
            **SectionOut(**_dump(node["section"])).model_dump(),
            passages=[PassageOut(**_dump(p)) for p in node["passages"]],
            media_assets=[MediaAssetOut(**_dump(m)) for m in node["media_assets"]],
            questions=[QuestionOut(**_dump(q)) for q, _k in node["questions"]],
            answer_keys=[AnswerKeyOut(**_dump(k)) for _q, k in node["questions"] if k is not None],
            writing_prompts=[WritingPromptOut(**_dump(w)) for w in node["writing_prompts"]],
            speaking_prompts=[SpeakingPromptOut(**_dump(sp)) for sp in node["speaking_prompts"]],
        )
        for node in tree
    ]
    return AdminTestContentOut(
        test=_to_test_out(test),
        sections=sections,
        publish_problems=validate_for_publish(session, test) if test.status == "draft" else [],
    )


@router.post("/admin/tests/{test_id}/archive", response_model=TestOut)
def archive_test(
    test_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TestOut:
    """Hides a published test from the student catalog. Its version stays
    published and immutable, so past attempts stay reproducible."""

    test = _get_test_or_404(session, test_id)
    if test.status != "published":
        raise HTTPException(status_code=409, detail="Only a published test can be archived.")
    test.status = "archived"
    session.add(test)
    session.commit()
    session.refresh(test)
    return _to_test_out(test)


@router.post("/admin/tests/{test_id}/restore", response_model=TestOut)
def restore_test(
    test_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TestOut:
    test = _get_test_or_404(session, test_id)
    if test.status != "archived":
        raise HTTPException(status_code=409, detail="Only an archived test can be restored.")
    test.status = "published"
    session.add(test)
    session.commit()
    session.refresh(test)
    return _to_test_out(test)


@router.delete("/admin/tests/{test_id}", status_code=204)
def delete_test(
    test_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    """Drafts only - a published version can't be deleted (the DB trigger
    rejects it too). Archive a published test instead."""

    test = _get_test_or_404(session, test_id)
    if test.status != "draft":
        raise HTTPException(status_code=409, detail="Only a draft test can be deleted. Archive a published test instead.")
    # Clear the tests -> test_versions pointer first so the cascade from
    # tests -> test_versions has an unambiguous order.
    test.current_version_id = None
    session.add(test)
    session.flush()
    session.delete(test)
    session.commit()


@router.get("/admin/tests/{test_id}/export")
def export_test_json(
    test_id: UUID,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    return export_test(session, _get_test_or_404(session, test_id))


@router.post("/admin/tests/{test_id}/duplicate", response_model=TestOut, status_code=201, dependencies=[Depends(per_user(admin_bulk_limiter))])
def duplicate_test(
    test_id: UUID,
    request: Request,
    _: str = Depends(require_admin),
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> TestOut:
    """How a published test gets edited: copy it into a new draft, fix it,
    publish the copy, archive the original."""

    source = _get_test_or_404(session, test_id)
    doc = export_test(session, source)
    doc["title"] = f"{source.title} (copy)"[:200]
    test = import_test(session, TestDoc.model_validate(doc), created_by=UUID(user_id))
    session.commit()
    session.refresh(test)
    audit_note(request, created_test_id=test.id, source_test_id=source.id)
    return _to_test_out(test)


@router.get("/admin/users", response_model=list[AdminUserOut])
def list_users(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[AdminUserOut]:
    """FR-ADMIN-06: high-level student overview only - no passwords (Auth
    owns those, not this table) and none of the more private profile
    fields (target_band, exam_date, timezone) that don't belong in a
    plain admin listing.
    """

    rows = session.exec(
        select(Profile, UserRole.role)
        .join(UserRole, UserRole.user_id == Profile.id, isouter=True)
        .order_by(Profile.created_at.desc())
    ).all()
    # FR-ADMIN-06's "high-level attempts": counts only, never answers.
    attempt_counts: dict[UUID, list[int]] = {}
    for student_id, status, count in session.exec(
        select(Attempt.student_id, Attempt.status, func.count()).group_by(Attempt.student_id, Attempt.status)
    ).all():
        totals = attempt_counts.setdefault(student_id, [0, 0])
        totals[0] += count
        if status == "submitted":
            totals[1] += count
    return [
        AdminUserOut(
            id=str(profile.id),
            email=profile.email,
            display_name=profile.display_name,
            role=role or "student",
            onboarding_completed=profile.onboarding_completed,
            created_at=profile.created_at,
            attempts_total=attempt_counts.get(profile.id, [0, 0])[0],
            attempts_submitted=attempt_counts.get(profile.id, [0, 0])[1],
        )
        for profile, role in rows
    ]

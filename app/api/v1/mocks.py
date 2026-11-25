"""Full mock examination endpoints (spec §8: /mocks, FR-MOCK-01..07)."""

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ...core.security import get_current_user_id
from ...db.session import get_session
from ...models import MockAttempt, Test, TestVersion
from ...schemas.mocks import MockAttemptStatusOut, MockCreate, MockOut, MockResultsOut
from ...services.mock_flow import (
    BAND_NOTE,
    MODULE_LABEL,
    MODULE_ORDER,
    TERMINAL_ATTEMPT_STATUSES,
    abandon_mock,
    certificate_for_mock,
    choose_mock_versions,
    complete_mock,
    find_open_mock,
    module_attempts,
    module_results,
    overall_band,
    reconcile_mock,
    require_owned_mock,
    to_certificate_summary,
    to_mock_out,
)
from ...services.rate_limit import mock_limiter, per_user
from .attempts import new_attempt, reconcile_expiry

router = APIRouter()

ModuleName = Literal["listening", "reading", "writing", "speaking"]


def _reconciled(session: Session, mock: MockAttempt) -> MockAttempt:
    return reconcile_mock(session, mock, reconcile_expiry)


@router.post("/mocks", response_model=MockOut, dependencies=[Depends(per_user(mock_limiter))])
def start_mock(
    body: MockCreate,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> MockOut:
    """FR-MOCK-01/04: starts a full mock, or returns the student's open one
    (whatever its track) so a reconnect never creates a second."""

    student_id = UUID(user_id)
    existing = find_open_mock(session, student_id)
    if existing is not None:
        existing = _reconciled(session, existing)
        if existing.status == "in_progress":
            return to_mock_out(session, existing)

    versions = choose_mock_versions(session, body.track)
    now = datetime.now(timezone.utc)
    mock = MockAttempt(
        id=uuid4(),
        student_id=student_id,
        track=body.track,
        status="in_progress",
        listening_version_id=versions["listening"],
        reading_version_id=versions["reading"],
        writing_version_id=versions["writing"],
        speaking_version_id=versions["speaking"],
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(mock)
    try:
        session.commit()
    except IntegrityError:
        # Lost a race with a concurrent start (one open mock per student,
        # enforced by a partial unique index): return the winner.
        session.rollback()
        winner = find_open_mock(session, student_id)
        if winner is None:
            raise
        return to_mock_out(session, winner)
    session.refresh(mock)
    return to_mock_out(session, mock)


@router.get("/mocks", response_model=list[MockOut])
def list_mocks(
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> list[MockOut]:
    mocks = session.exec(
        select(MockAttempt)
        .where(MockAttempt.student_id == UUID(user_id))
        .order_by(MockAttempt.started_at.desc())
        .limit(50)
    ).all()
    return [to_mock_out(session, _reconciled(session, mock)) for mock in mocks]


@router.get("/mocks/{mock_id}", response_model=MockOut)
def get_mock(
    mock_id: UUID,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> MockOut:
    mock = _reconciled(session, require_owned_mock(session, mock_id, UUID(user_id)))
    return to_mock_out(session, mock)


@router.post("/mocks/{mock_id}/modules/{module}/start", response_model=MockAttemptStatusOut, dependencies=[Depends(per_user(mock_limiter))])
def start_mock_module(
    mock_id: UUID,
    module: ModuleName,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> MockAttemptStatusOut:
    """FR-MOCK-02/03/04: starts (or resumes) one module, in order, as an
    exam-mode attempt against the version pinned for this mock. Calling it
    again returns the same attempt - never a duplicate."""

    student_id = UUID(user_id)
    mock = require_owned_mock(session, mock_id, student_id, lock=True)
    mock = _reconciled(session, mock)
    attempts = module_attempts(session, mock)

    existing = attempts[module]
    if existing is not None:
        existing = reconcile_expiry(session, existing)
        version = session.get(TestVersion, existing.test_version_id)
        return MockAttemptStatusOut(
            mock_id=str(mock.id), module=module, attempt_id=str(existing.id), test_id=str(version.test_id), status=existing.status
        )

    if mock.status != "in_progress":
        raise HTTPException(status_code=409, detail=f"This mock is {mock.status}.")
    for earlier in MODULE_ORDER[: MODULE_ORDER.index(module)]:
        attempt = attempts[earlier]
        if attempt is None or attempt.status not in TERMINAL_ATTEMPT_STATUSES:
            raise HTTPException(status_code=409, detail=f"Finish {MODULE_LABEL[earlier]} before starting {MODULE_LABEL[module]}.")

    version_id = getattr(mock, f"{module}_version_id")
    version = session.get(TestVersion, version_id)
    test = session.get(Test, version.test_id) if version else None
    if test is None:
        raise HTTPException(status_code=404, detail=f"The {MODULE_LABEL[module]} test in this mock no longer exists.")

    attempt = new_attempt(student_id, test, version_id, "exam")
    session.add(attempt)
    session.flush()
    setattr(mock, f"{module}_attempt_id", attempt.id)
    mock.updated_at = datetime.now(timezone.utc)
    session.add(mock)
    session.commit()
    return MockAttemptStatusOut(
        mock_id=str(mock.id), module=module, attempt_id=str(attempt.id), test_id=str(test.id), status=attempt.status
    )


@router.post("/mocks/{mock_id}/complete", response_model=MockOut)
def finish_mock(
    mock_id: UUID,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> MockOut:
    """FR-MOCK-07: validates that all four modules are finished, then
    completes the mock and issues its certificate. Idempotent."""

    mock = require_owned_mock(session, mock_id, UUID(user_id), lock=True)
    mock = _reconciled(session, mock)
    mock = complete_mock(session, mock)
    return to_mock_out(session, mock)


@router.post("/mocks/{mock_id}/abandon", response_model=MockOut)
def abandon(
    mock_id: UUID,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> MockOut:
    """Ends an unfinished mock so a new one can be started. Any module in
    progress is cancelled (no score, no certificate)."""

    mock = require_owned_mock(session, mock_id, UUID(user_id), lock=True)
    mock = _reconciled(session, mock)
    mock = abandon_mock(session, mock)
    return to_mock_out(session, mock)


@router.get("/mocks/{mock_id}/results", response_model=MockResultsOut)
def get_mock_results(
    mock_id: UUID,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> MockResultsOut:
    """FR-MOCK-05/06: consolidated, honest results for a completed mock."""

    mock = _reconciled(session, require_owned_mock(session, mock_id, UUID(user_id)))
    if mock.status != "completed":
        raise HTTPException(status_code=409, detail="Results are available once every module is finished.")
    results = module_results(session, mock)
    band, note = overall_band(results)
    return MockResultsOut(
        id=str(mock.id),
        track=mock.track,
        status=mock.status,
        started_at=mock.started_at,
        completed_at=mock.completed_at,
        modules=results,
        overall_band=band,
        overall_band_note=note,
        band_note=BAND_NOTE,
        certificate=to_certificate_summary(certificate_for_mock(session, mock.id)),
    )

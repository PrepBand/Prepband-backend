"""Full mock state machine (FR-MOCK-01..07) and certificate issuing.

Lifecycle of a mock_attempts row:

    in_progress --(all four module attempts submitted/expired)--> completed
    in_progress --(student abandons)--> abandoned

Modules run strictly in MODULE_ORDER. Each module is an ordinary attempt
(same timer, autosave, submit and grading as single-module practice),
taken in exam mode against the version pinned when the mock started.
Completion is idempotent and issues exactly one certificate.
"""

import secrets
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from ..models import (
    Attempt,
    Certificate,
    MockAttempt,
    Profile,
    Section,
    SpeakingPrompt,
    SpeakingResponse,
    Test,
    TestVersion,
    WritingPrompt,
    WritingResponse,
)
from ..schemas.mocks import CertificateSummary, MockModuleOut, MockModuleResult, MockOut

MODULE_ORDER = ("listening", "reading", "writing", "speaking")
MODULE_LABEL = {"listening": "Listening", "reading": "Reading", "writing": "Writing", "speaking": "Speaking"}
TERMINAL_ATTEMPT_STATUSES = {"submitted", "expired"}

BAND_NOTE = (
    "Listening and Reading bands are approximate estimates from a simple percentage conversion, "
    "not official IELTS scores."
)

# Unambiguous characters only (no I, L, O, 0, 1): 31 symbols x 16 = ~79 bits.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_certificate_code() -> str:
    chars = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(16))
    return "PB-" + "-".join(chars[i : i + 4] for i in range(0, 16, 4))


def normalize_certificate_code(raw: str) -> str:
    return raw.strip().upper()


# --- lookups ----------------------------------------------------------------


def require_owned_mock(session: Session, mock_id: UUID, student_id: UUID, lock: bool = False) -> MockAttempt:
    query = select(MockAttempt).where(MockAttempt.id == mock_id)
    if lock:
        # populate_existing: re-locking later in the same request must see
        # what other requests committed meanwhile, not the cached row.
        query = query.with_for_update().execution_options(populate_existing=True)
    mock = session.exec(query).first()
    # 404 rather than 403 for someone else's mock: don't confirm it exists.
    if mock is None or mock.student_id != student_id:
        raise HTTPException(status_code=404, detail="Mock test not found.")
    return mock


def module_attempts(session: Session, mock: MockAttempt) -> dict[str, Attempt | None]:
    result: dict[str, Attempt | None] = {}
    for module in MODULE_ORDER:
        attempt_id = getattr(mock, f"{module}_attempt_id")
        result[module] = session.get(Attempt, attempt_id) if attempt_id else None
    return result


def module_tests(session: Session, mock: MockAttempt) -> dict[str, Test]:
    result: dict[str, Test] = {}
    for module in MODULE_ORDER:
        version = session.get(TestVersion, getattr(mock, f"{module}_version_id"))
        test = session.get(Test, version.test_id) if version else None
        if test is None:
            raise HTTPException(status_code=404, detail=f"The {MODULE_LABEL[module]} test in this mock no longer exists.")
        result[module] = test
    return result


def choose_mock_versions(session: Session, track: str) -> dict[str, UUID]:
    """The newest published *mock* test per module for this track (or one
    shared by both tracks). Every missing module is reported at once."""

    chosen: dict[str, UUID] = {}
    missing: list[str] = []
    for module in MODULE_ORDER:
        test = session.exec(
            select(Test)
            .where(
                Test.status == "published",
                Test.kind == "mock",
                Test.module == module,
                Test.track.in_([track, "both"]),
                Test.current_version_id.is_not(None),
            )
            .order_by(Test.created_at.desc())
        ).first()
        if test is None:
            missing.append(f"No {MODULE_LABEL[module]} mock test is published for the {track.title()} track yet.")
        else:
            chosen[module] = test.current_version_id
    if missing:
        raise HTTPException(
            status_code=409,
            detail={"message": "A full mock isn't available for this track yet.", "problems": missing},
        )
    return chosen


def find_open_mock(session: Session, student_id: UUID) -> MockAttempt | None:
    return session.exec(
        select(MockAttempt).where(MockAttempt.student_id == student_id, MockAttempt.status == "in_progress")
    ).first()


def certificate_for_mock(session: Session, mock_id: UUID) -> Certificate | None:
    return session.exec(select(Certificate).where(Certificate.mock_attempt_id == mock_id)).first()


def holder_name(session: Session, student_id: UUID) -> str:
    profile = session.get(Profile, student_id)
    if profile is None:
        return "Prepband student"
    name = (profile.display_name or "").strip()
    return name or profile.email.split("@")[0]


def to_certificate_summary(certificate: Certificate | None) -> CertificateSummary | None:
    if certificate is None:
        return None
    return CertificateSummary(
        id=str(certificate.id),
        code=certificate.code,
        issued_at=certificate.issued_at,
        revoked_at=certificate.revoked_at,
        public_verification=certificate.public_verification,
    )


# --- state transitions ---------------------------------------------------------


def _lock_mock(session: Session, mock: MockAttempt) -> MockAttempt:
    return session.exec(
        select(MockAttempt).where(MockAttempt.id == mock.id).with_for_update().execution_options(populate_existing=True)
    ).one()


def _lock_attempt(session: Session, attempt: Attempt) -> Attempt:
    return session.exec(
        select(Attempt).where(Attempt.id == attempt.id).with_for_update().execution_options(populate_existing=True)
    ).one()


def reconcile_mock(session: Session, mock: MockAttempt, reconcile_attempt) -> MockAttempt:
    """Brings an open mock up to date: expires any module attempt whose
    deadline has passed (via the attempt engine's own reconcile), then
    completes the mock if all four modules are now finished. Safe to call
    on every read - that is what makes refresh/reconnect work (FR-MOCK-04)."""

    if mock.status != "in_progress":
        return mock
    attempts = module_attempts(session, mock)
    for attempt in attempts.values():
        if attempt is not None and attempt.status == "in_progress":
            reconcile_attempt(session, attempt)
    if all(a is not None and a.status in TERMINAL_ATTEMPT_STATUSES for a in attempts.values()):
        return complete_mock(session, mock)
    return mock


def unfinished_modules(session: Session, mock: MockAttempt) -> list[str]:
    attempts = module_attempts(session, mock)
    return [m for m in MODULE_ORDER if attempts[m] is None or attempts[m].status not in TERMINAL_ATTEMPT_STATUSES]


def complete_mock(session: Session, mock: MockAttempt) -> MockAttempt:
    """FR-MOCK-07 / FR-CERT-07: completes the mock and issues its one
    certificate in a single transaction. Eligibility comes only from the
    server's own attempt records. Idempotent."""

    # Lock and re-read first: two reads (GET /mocks/{id}) can both see four
    # finished modules, and the second "completed" update would hit
    # mock_attempts_immutable_once_terminal. The second caller waits here
    # and then sees the first one's result.
    mock = _lock_mock(session, mock)
    if mock.status == "completed":
        return mock
    if mock.status != "in_progress":
        raise HTTPException(status_code=409, detail=f"This mock was {mock.status} and can't be completed.")
    remaining = unfinished_modules(session, mock)
    if remaining:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Finish every module before completing the mock.",
                "problems": [f"{MODULE_LABEL[m]} isn't finished yet." for m in remaining],
            },
        )

    now = datetime.now(timezone.utc)
    mock.status = "completed"
    mock.completed_at = now
    mock.updated_at = now
    session.add(mock)
    if certificate_for_mock(session, mock.id) is None:
        session.add(
            Certificate(
                id=uuid4(),
                code=generate_certificate_code(),
                mock_attempt_id=mock.id,
                student_id=mock.student_id,
                issued_at=now,
                public_verification=False,
                created_at=now,
                updated_at=now,
            )
        )
    try:
        session.commit()
    except IntegrityError:
        # A concurrent request completed it first; use what it wrote.
        session.rollback()
    session.refresh(mock)
    return mock


def abandon_mock(session: Session, mock: MockAttempt) -> MockAttempt:
    # Reconciling before this may have committed (releasing the caller's
    # lock), so lock and re-read before deciding.
    mock = _lock_mock(session, mock)
    if mock.status == "abandoned":
        return mock
    if mock.status == "completed":
        raise HTTPException(status_code=409, detail="A completed mock can't be abandoned.")
    now = datetime.now(timezone.utc)
    for attempt in module_attempts(session, mock).values():
        if attempt is None:
            continue
        # Lock and re-read: a submit that is finishing right now holds this
        # lock, and once it commits the attempt is final and must be left
        # alone (cancelling it would hit the final-attempt trigger).
        attempt = _lock_attempt(session, attempt)
        if attempt.status == "in_progress":
            attempt.status = "cancelled"
            attempt.updated_at = now
            session.add(attempt)
    mock.status = "abandoned"
    mock.updated_at = now
    session.add(mock)
    session.commit()
    session.refresh(mock)
    return mock


# --- read models ---------------------------------------------------------------


def to_mock_out(session: Session, mock: MockAttempt) -> MockOut:
    attempts = module_attempts(session, mock)
    tests = module_tests(session, mock)
    next_module = None
    if mock.status == "in_progress":
        next_module = next(
            (m for m in MODULE_ORDER if attempts[m] is None or attempts[m].status not in TERMINAL_ATTEMPT_STATUSES),
            None,
        )
    modules = []
    for module in MODULE_ORDER:
        attempt, test = attempts[module], tests[module]
        modules.append(
            MockModuleOut(
                module=module,
                test_id=str(test.id),
                test_title=test.title,
                duration_minutes=test.duration_minutes,
                attempt_id=str(attempt.id) if attempt else None,
                status=attempt.status if attempt else "not_started",
                started_at=attempt.started_at if attempt else None,
                expires_at=attempt.expires_at if attempt else None,
                submitted_at=attempt.submitted_at if attempt else None,
                is_next=module == next_module,
            )
        )
    return MockOut(
        id=str(mock.id),
        track=mock.track,
        status=mock.status,
        started_at=mock.started_at,
        completed_at=mock.completed_at,
        modules=modules,
        completed_modules=sum(1 for a in attempts.values() if a is not None and a.status in TERMINAL_ATTEMPT_STATUSES),
        next_module=next_module,
        certificate=to_certificate_summary(certificate_for_mock(session, mock.id)),
    )


def _prompt_count(session: Session, model, version_id: UUID) -> int:
    section_ids = [s.id for s in session.exec(select(Section).where(Section.test_version_id == version_id)).all()]
    if not section_ids:
        return 0
    return session.exec(select(func.count()).select_from(model).where(model.section_id.in_(section_ids))).one()


def module_results(session: Session, mock: MockAttempt) -> list[MockModuleResult]:
    """FR-MOCK-05 / FR-RESULT-04: per-module outcome with explicit provenance."""

    attempts = module_attempts(session, mock)
    tests = module_tests(session, mock)
    results = []
    for module in MODULE_ORDER:
        attempt, test = attempts[module], tests[module]
        version_id = getattr(mock, f"{module}_version_id")
        result = MockModuleResult(
            module=module,
            test_title=test.title,
            attempt_id=str(attempt.id) if attempt else None,
            status=attempt.status if attempt else "not_started",
            mode=attempt.mode if attempt else None,
            assessment_status="not_assessed" if module in ("writing", "speaking") else "objective",
            raw_score=attempt.raw_score if attempt else None,
            max_score=attempt.max_score if attempt else None,
            estimated_band=attempt.estimated_band if attempt else None,
            submitted_at=attempt.submitted_at if attempt else None,
        )
        if module == "writing":
            rows = session.exec(select(WritingResponse).where(WritingResponse.attempt_id == attempt.id)).all() if attempt else []
            result.words_written = sum(r.word_count for r in rows)
            result.tasks_answered = sum(1 for r in rows if r.word_count > 0)
            result.tasks_total = _prompt_count(session, WritingPrompt, version_id)
        elif module == "speaking":
            rows = session.exec(select(SpeakingResponse).where(SpeakingResponse.attempt_id == attempt.id)).all() if attempt else []
            result.speaking_answers = sum(1 for r in rows if r.completed)
            result.speaking_prompts_total = _prompt_count(session, SpeakingPrompt, version_id)
        results.append(result)
    return results


def overall_band(results: list[MockModuleResult]) -> tuple[float | None, str]:
    """FR-MOCK-06: an overall band only exists if every module has a valid
    band. Writing and Speaking are never assessed in the MVP, so in
    practice this always returns None with an explanation."""

    missing = [MODULE_LABEL[r.module] for r in results if r.estimated_band is None]
    if missing:
        if set(missing) == {"Writing", "Speaking"}:
            reason = "Writing and Speaking are not assessed by Prepband"
        else:
            reason = f"{', '.join(missing)} {'has' if len(missing) == 1 else 'have'} no band"
        return None, f"Overall band: Not available. {reason}, so an overall band can't be calculated honestly."

    # IELTS-style rounding of the mean: .25 rounds up to .5, .75 up to the next whole band.
    mean = sum(r.estimated_band for r in results) / len(results)
    whole = int(mean)
    fraction = mean - whole
    band = whole + (0.0 if fraction < 0.25 else 0.5 if fraction < 0.75 else 1.0)
    return band, "Overall band is an unofficial estimate from estimated module bands."

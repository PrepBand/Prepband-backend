"""Test harness: the real FastAPI app against a throwaway in-memory SQLite
database, with JWT verification swapped for an `X-Test-User` header.

SQLite has none of the Postgres triggers/RLS/partial indexes from
database/migrations - those are verified against the live database. This
suite covers the API's own rules: ownership, ordering, state transitions,
grading provenance and certificate eligibility.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.v1.attempts import clear_section_cache
from app.core.security import get_current_user_id
from app.db.session import get_session
from app.main import app
from app.models import (
    AnswerKey,
    Profile,
    Question,
    Section,
    SpeakingPrompt,
    TestVersion,
    UserRole,
    WritingPrompt,
)
from app.models import (
    Test as TestRow,
)
from app.services.audit import reset_audit_limits
from app.services.rate_limit import reset_all_limiters


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture()
def client(engine):
    def _session():
        with Session(engine) as s:
            yield s

    def _user(x_test_user: str | None = Header(default=None)) -> str:
        if not x_test_user:
            raise HTTPException(status_code=401, detail="Missing bearer token.")
        return x_test_user

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user_id] = _user
    reset_all_limiters()
    reset_audit_limits()
    clear_section_cache()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make_user(session: Session, name: str = "Amina Khan", role: str = "student") -> str:
    user_id = uuid4()
    now = _now()
    session.add(Profile(id=user_id, display_name=name, email=f"{user_id.hex[:8]}@example.test", created_at=now, updated_at=now))
    session.add(UserRole(user_id=user_id, role=role, created_at=now, updated_at=now))
    session.commit()
    return str(user_id)


def as_user(user_id: str) -> dict[str, str]:
    return {"X-Test-User": user_id}


def publish_test(session: Session, module: str, track: str, kind: str = "mock", duration: int | None = 30) -> TestRow:
    """A small published test for one module: two objective questions for
    Listening/Reading, two tasks for Writing, three parts for Speaking."""

    now = _now()
    test_id, version_id, section_id = uuid4(), uuid4(), uuid4()
    # Inserted in foreign-key order (test, version, section, question, key)
    # so the same helper works on real Postgres; current_version_id is set
    # once the version exists.
    test = TestRow(
        id=test_id,
        title=f"{module.title()} {kind} ({track})",
        module=module,
        track=track,
        kind=kind,
        duration_minutes=duration,
        status="published",
        current_version_id=None,
        created_at=now,
        updated_at=now,
    )
    session.add(test)
    session.flush()
    session.add(TestVersion(id=version_id, test_id=test_id, version_number=1, status="published", published_at=now, created_at=now))
    session.flush()
    test.current_version_id = version_id
    session.add(test)
    session.add(Section(id=section_id, test_version_id=version_id, order_index=1, module=module, created_at=now))
    session.flush()
    if module in ("listening", "reading"):
        for order, answer in enumerate(["London", "Tuesday"], start=1):
            question_id = uuid4()
            session.add(
                Question(id=question_id, section_id=section_id, type="short_answer", prompt=f"Q{order}", display_order=order, marks=1, created_at=now)
            )
            session.flush()
            session.add(AnswerKey(id=uuid4(), question_id=question_id, correct_answers=[answer], created_at=now))
    elif module == "writing":
        for task in (1, 2):
            session.add(
                WritingPrompt(
                    id=uuid4(), section_id=section_id, task_number=task, prompt=f"Task {task}", min_words=150 if task == 1 else 250,
                    display_order=task, created_at=now,
                )
            )
    else:
        for part in (1, 2, 3):
            session.add(SpeakingPrompt(id=uuid4(), section_id=section_id, part=part, prompt=f"Part {part}", display_order=part, created_at=now))
    session.commit()
    session.refresh(test)
    return test


def publish_full_mock(session: Session, track: str = "academic") -> dict[str, TestRow]:
    return {
        "listening": publish_test(session, "listening", "both"),
        "reading": publish_test(session, "reading", track),
        "writing": publish_test(session, "writing", track),
        "speaking": publish_test(session, "speaking", "both"),
    }


def uid(value: str) -> UUID:
    return UUID(value)

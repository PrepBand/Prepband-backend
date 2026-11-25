"""Fixtures for the Postgres test suite.

The main suite runs on SQLite, which can't run the database's own rules:
triggers, row-level security, grants and row locks. These tests build a
real Postgres database from database/migrations (plus a small stub for the
Supabase parts, supabase_stub.sql) and check those rules directly.

Where the database comes from, in order:
1. TEST_DATABASE_URL (CI starts a postgres service container), or
2. the optional `pgserver` package, which runs a throwaway local Postgres
   (`pip install pgserver`), or
3. neither: every test here is skipped.

Each test run creates a fresh database and drops it at the end.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi import Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, create_engine

from app.api.v1.attempts import clear_section_cache
from app.core.security import get_current_user_id
from app.db.session import get_session
from app.main import app
from app.services.audit import reset_audit_limits
from app.services.rate_limit import reset_all_limiters

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "database" / "migrations"
STUB = Path(__file__).with_name("supabase_stub.sql")


def _server_url(tmp_root: Path) -> str | None:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    try:
        import pgserver  # type: ignore[import-not-found]
    except ImportError:
        return None
    server = pgserver.get_server(str(tmp_root / "pgdata"), cleanup_mode="stop")
    return server.get_uri()


def _with_database(url: str, name: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{name}"


def _psycopg_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture(scope="session")
def pg_url(tmp_path_factory) -> Iterator[str]:
    server = _server_url(tmp_path_factory.mktemp("pg"))
    if server is None:
        pytest.skip("No Postgres: set TEST_DATABASE_URL or `pip install pgserver`.")
    name = f"prepband_test_{uuid.uuid4().hex[:10]}"
    admin_url = _psycopg_url(server)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'create database "{name}"')
    url = _with_database(admin_url, name)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(STUB.read_text(encoding="utf-8"))
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            try:
                conn.execute(migration.read_text(encoding="utf-8"))
            except psycopg.Error as exc:  # pragma: no cover - the message is the point
                raise RuntimeError(f"{migration.name} failed to apply: {exc}") from exc
    yield url.replace("postgresql://", "postgresql+psycopg://", 1)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'drop database if exists "{name}" with (force)')


@pytest.fixture(scope="session")
def pg_engine(pg_url) -> Iterator[Engine]:
    engine = create_engine(pg_url, pool_size=10, max_overflow=10)
    yield engine
    engine.dispose()


@pytest.fixture()
def pg(pg_url, pg_engine) -> Iterator[psycopg.Connection]:
    """A raw autocommit connection as the database owner (like the backend's
    DATABASE_URL), with every table emptied before the test."""

    url = pg_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url, autocommit=True) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "select format('%I.%I', schemaname, tablename) from pg_tables where schemaname = 'public'"
            ).fetchall()
        ]
        # TRUNCATE doesn't fire row triggers, so frozen attempts empty too.
        conn.execute(f"truncate {', '.join(tables + ['auth.users'])} cascade")
        yield conn


@pytest.fixture()
def pg_session(pg, pg_engine) -> Iterator[Session]:
    with Session(pg_engine) as session:
        yield session


@pytest.fixture()
def pg_client(pg, pg_engine) -> Iterator[TestClient]:
    def _session():
        with Session(pg_engine) as session:
            yield session

    def _user(x_test_user: str | None = Header(default=None)) -> str:
        if not x_test_user:
            raise HTTPException(status_code=401, detail="Missing bearer token.")
        return x_test_user

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_user_id] = _user
    reset_all_limiters()
    reset_audit_limits()
    clear_section_cache()
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def create_user(conn: psycopg.Connection, name: str = "Amina Khan", role: str = "student") -> str:
    """Signs a user up the way Supabase does: a row in auth.users, which the
    Phase 1 triggers turn into a profile and a student role."""

    user_id = str(uuid.uuid4())
    conn.execute(
        "insert into auth.users (id, email, raw_user_meta_data) values (%s, %s, %s::jsonb)",
        (user_id, f"{user_id[:8]}@example.test", f'{{"full_name": "{name}"}}'),
    )
    if role != "student":
        conn.execute("update public.user_roles set role = %s where user_id = %s", (role, user_id))
    return user_id


def as_role(conn: psycopg.Connection, role: str, user_id: str | None = None) -> None:
    """Switches the connection to an API role, as PostgREST does for a
    browser request (with the caller's id in the JWT claim)."""

    conn.execute(f"set role {role}")
    conn.execute("select set_config('request.jwt.claim.sub', %s, false)", (user_id or "",))


def reset_role(conn: psycopg.Connection) -> None:
    conn.execute("reset role")
    conn.execute("select set_config('request.jwt.claim.sub', '', false)")

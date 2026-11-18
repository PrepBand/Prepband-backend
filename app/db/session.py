from collections.abc import Generator

from fastapi import HTTPException
from sqlalchemy import Engine
from sqlmodel import Session, create_engine

from ..core.config import get_settings

_engine: Engine | None = None


def _get_engine() -> Engine | None:
    global _engine
    if _engine is None:
        settings = get_settings()
        if not settings.database_url:
            return None
        _engine = create_engine(settings.database_url, pool_pre_ping=True)
    return _engine


def get_session() -> Generator[Session, None, None]:
    """Yields a DB session, or a clear 503 if DATABASE_URL isn't set yet.

    Schema (tables, RLS, triggers) is owned by Supabase migrations, applied
    via the Supabase MCP - never call SQLModel.metadata.create_all() against
    this engine.
    """

    engine = _get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Database is not configured yet.")
    with Session(engine) as session:
        yield session

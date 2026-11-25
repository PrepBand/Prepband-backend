"""Resource library for signed-in students: categories, search and
filters, and short-lived links to open a resource.

Only published resources in visible categories are ever returned. Files
and covers sit in a private R2 bucket; every URL here is signed and
expires (covers after an hour, files after five minutes), so a link pasted
elsewhere soon stops working.

Search: on Postgres, weighted full-text search (title, tags, description,
category) plus trigram similarity on the title when pg_trgm is installed,
so "vocabluary" still finds "Vocabulary". On SQLite (tests) a plain
case-insensitive match stands in.
"""

import json
import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import String, cast, func, literal_column, or_, text, update
from sqlmodel import Session, select

from ...core.security import get_current_user_id
from ...db.session import get_session
from ...models import Resource, ResourceCategory
from ...services import r2
from ...services.rate_limit import RateLimiter, per_user

router = APIRouter()
logger = logging.getLogger("prepband.resources")

open_limiter = RateLimiter(60, 60, "Too many resources opened in a short time. Please wait a moment.")

COVER_URL_SECONDS = 3600
FILE_URL_SECONDS = 300
INLINE_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}


class CategoryOut(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None
    resource_count: int


class ResourceCard(BaseModel):
    id: str
    title: str
    description: str | None
    kind: Literal["link", "video", "pdf", "document", "image"]
    module: str
    track: str
    level: str | None
    tags: list[str]
    category_slug: str
    category_name: str
    cover_url: str | None
    cover_alt: str | None
    file_name: str | None
    mime_type: str | None
    size_bytes: int | None
    is_external: bool
    published_at: datetime | None


class ResourcePage(BaseModel):
    items: list[ResourceCard]
    total: int
    query: str | None


class OpenOut(BaseModel):
    url: str
    expires_in: int | None


def _visible_published():
    return (
        select(Resource, ResourceCategory)
        .join(ResourceCategory, ResourceCategory.id == Resource.category_id)
        .where(Resource.status == "published", ResourceCategory.is_visible.is_(True))
    )


_TRGM: dict[str, bool] = {}


def _has_trigram(session: Session) -> bool:
    url = str(session.get_bind().url)
    if url not in _TRGM:
        _TRGM[url] = bool(session.exec(text("select exists(select 1 from pg_extension where extname = 'pg_trgm')")).one()[0])
    return _TRGM[url]


def to_card(resource: Resource, category: ResourceCategory, bucket: r2.Bucket | None) -> ResourceCard:
    return ResourceCard(
        id=str(resource.id),
        title=resource.title,
        description=resource.description,
        kind=resource.kind,
        module=resource.module,
        track=resource.track,
        level=resource.level,
        tags=list(resource.tags or []),
        category_slug=category.slug,
        category_name=category.name,
        cover_url=bucket.download_url(resource.cover_key, expires=COVER_URL_SECONDS) if bucket and resource.cover_key else None,
        cover_alt=resource.cover_alt,
        file_name=resource.file_name,
        mime_type=resource.mime_type,
        size_bytes=resource.size_bytes,
        is_external=resource.kind in ("link", "video"),
        published_at=resource.published_at,
    )


@router.get("/resource-categories", response_model=list[CategoryOut])
def list_categories(
    _: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> list[CategoryOut]:
    counts = dict(
        session.exec(
            select(Resource.category_id, func.count()).where(Resource.status == "published").group_by(Resource.category_id)
        ).all()
    )
    categories = session.exec(
        select(ResourceCategory).where(ResourceCategory.is_visible.is_(True)).order_by(ResourceCategory.display_order, ResourceCategory.name)
    ).all()
    return [
        CategoryOut(id=str(c.id), name=c.name, slug=c.slug, description=c.description, resource_count=counts.get(c.id, 0))
        for c in categories
    ]


@router.get("/resources", response_model=ResourcePage)
def list_resources(
    q: str | None = Query(default=None, max_length=100),
    category: str | None = Query(default=None, max_length=60, pattern=r"^[a-z0-9-]+$"),
    module: str | None = Query(default=None, pattern="^(listening|reading|writing|speaking|general)$"),
    track: str | None = Query(default=None, pattern="^(academic|general)$"),
    kind: str | None = Query(default=None, pattern="^(link|video|pdf|document|image)$"),
    limit: int = Query(default=24, ge=1, le=60),
    offset: int = Query(default=0, ge=0),
    _: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> ResourcePage:
    query = _visible_published()
    if category:
        query = query.where(ResourceCategory.slug == category)
    if module:
        query = query.where(Resource.module == module)
    if track:
        # "both" resources suit either track.
        query = query.where(Resource.track.in_([track, "both"]))
    if kind:
        query = query.where(Resource.kind == kind)

    term = (q or "").strip()
    order = [Resource.display_order, Resource.published_at.desc()]
    if term:
        if session.get_bind().dialect.name == "postgresql":
            ts_query = func.websearch_to_tsquery("english", term)
            search_col = literal_column("resources.search")
            matches = [search_col.op("@@")(ts_query)]
            rank = func.ts_rank(search_col, ts_query)
            if _has_trigram(session):
                similarity = func.extensions.word_similarity(term, Resource.title)
                matches.append(similarity > 0.4)
                rank = rank + similarity
            query = query.where(or_(*matches))
            order = [rank.desc(), *order]
        else:
            like = f"%{term.lower()}%"
            query = query.where(
                or_(
                    func.lower(Resource.title).like(like),
                    func.lower(func.coalesce(Resource.description, "")).like(like),
                    func.lower(cast(Resource.tags, String)).like(like),
                    func.lower(ResourceCategory.name).like(like),
                )
            )

    total = session.exec(select(func.count()).select_from(query.subquery())).one()
    if term and total == 0:
        # Anonymous: what students look for that the library doesn't have.
        logger.info("resource_search_miss %s", json.dumps({"q": term[:80], "category": category, "module": module}))

    bucket = r2.get_bucket()
    rows = session.exec(query.order_by(*order).offset(offset).limit(limit)).all()
    return ResourcePage(items=[to_card(res, cat, bucket) for res, cat in rows], total=total, query=term or None)


@router.post("/resources/{resource_id}/open", response_model=OpenOut, dependencies=[Depends(per_user(open_limiter))])
def open_resource(
    resource_id: UUID,
    _: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> OpenOut:
    """A link to the resource: the external address for links and videos, a
    five-minute signed URL for files. Counts the open for admin analytics."""

    row = session.exec(_visible_published().where(Resource.id == resource_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Resource not found.")
    resource, _category = row
    if resource.kind in ("link", "video"):
        result = OpenOut(url=resource.external_url or "", expires_in=None)
    else:
        if not resource.file_key:
            raise HTTPException(status_code=404, detail="Resource not found.")
        bucket = r2.require_bucket()
        inline = (resource.mime_type or "") in INLINE_TYPES
        result = OpenOut(
            url=bucket.download_url(resource.file_key, file_name=resource.file_name, inline=inline, expires=FILE_URL_SECONDS),
            expires_in=FILE_URL_SECONDS,
        )
    session.execute(update(Resource).where(Resource.id == resource.id).values(open_count=Resource.open_count + 1))
    session.commit()
    return result

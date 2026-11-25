"""Admin: the resource library (categories, resources, file uploads).

Every route is under /admin, so the audit middleware logs each change.

Uploads go straight from the admin's browser to the private R2 bucket:
  1. POST /admin/resources/uploads checks the type and size and returns a
     10-minute signed PUT URL bound to that exact type and size.
  2. The browser PUTs the file to R2.
  3. Saving the resource with the returned key makes the API read the
     object's first bytes and confirm it really is that type; a mismatch
     deletes the object and refuses the save.
Replacing or deleting a file deletes the old object (best effort).

A resource can only be published when it's complete: a cover with alt
text, and a link (link/video) or a file (pdf/document/image) of the right
type. The database enforces the same rule.
"""

import re
import unicodedata
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_
from sqlmodel import Session, select

from ...core.roles import require_admin
from ...core.security import get_current_user_id
from ...db.session import get_session
from ...models import Resource, ResourceCategory
from ...services import r2
from ...services.audit import audit_note
from ...services.rate_limit import admin_bulk_limiter, per_user

router = APIRouter()

Kind = Literal["link", "video", "pdf", "document", "image"]
FILE_KINDS = {"pdf", "document", "image"}
KIND_TYPES = {
    "pdf": {"application/pdf"},
    "document": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    },
    "image": {"image/jpeg", "image/png", "image/webp"},
}
COVER_EXT_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def slugify(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:60]


# --- schemas -------------------------------------------------------------------


class CategoryIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    slug: str | None = Field(default=None, max_length=60, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    description: str | None = Field(default=None, max_length=300)
    display_order: int = Field(default=0, ge=0, le=100000)
    is_visible: bool = True


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=60)
    slug: str | None = Field(default=None, max_length=60, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    description: str | None = Field(default=None, max_length=300)
    display_order: int | None = Field(default=None, ge=0, le=100000)
    is_visible: bool | None = None


class AdminCategoryOut(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None
    display_order: int
    is_visible: bool
    resource_count: int
    published_count: int


def _clean_tags(tags: list[str]) -> list[str]:
    seen: list[str] = []
    for tag in tags:
        value = " ".join(tag.strip().lower().split())[:40]
        if value and value not in seen:
            seen.append(value)
    if len(seen) > 12:
        raise ValueError("At most 12 tags.")
    return seen


def _check_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or "." not in parts.hostname:
        raise ValueError("Links must be full https:// addresses.")
    if parts.username or parts.password:
        raise ValueError("Links can't contain a username or password.")
    if len(value) > 2000:
        raise ValueError("That link is too long.")
    return value


class ResourceFields(BaseModel):
    category_id: UUID | None = None
    title: str | None = Field(default=None, min_length=3, max_length=140)
    description: str | None = Field(default=None, max_length=1000)
    kind: Kind | None = None
    module: Literal["listening", "reading", "writing", "speaking", "general"] | None = None
    track: Literal["academic", "general", "both"] | None = None
    level: Literal["beginner", "intermediate", "advanced"] | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    cover_key: str | None = Field(default=None, max_length=120)
    cover_alt: str | None = Field(default=None, max_length=200)
    file_key: str | None = Field(default=None, max_length=120)
    file_name: str | None = Field(default=None, max_length=200)
    file_content_type: str | None = Field(default=None, max_length=120)
    external_url: str | None = Field(default=None, max_length=2000)
    source: str | None = Field(default=None, max_length=300)
    licence: str | None = Field(default=None, max_length=120)
    display_order: int | None = Field(default=None, ge=0, le=100000)

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_tags(value)

    @field_validator("external_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return _check_url(value)


class ResourceCreate(ResourceFields):
    category_id: UUID
    title: str = Field(min_length=3, max_length=140)
    kind: Kind


class StatusIn(BaseModel):
    status: Literal["draft", "published", "archived"]


class UploadIn(BaseModel):
    purpose: Literal["cover", "file"]
    file_name: str = Field(min_length=1, max_length=200)
    content_type: str = Field(max_length=120)
    size_bytes: int = Field(gt=0)


class UploadOut(BaseModel):
    key: str
    upload_url: str
    headers: dict[str, str]
    expires_in: int


class AdminResourceOut(BaseModel):
    id: str
    category_id: str
    category_name: str
    title: str
    description: str | None
    kind: Kind
    module: str
    track: str
    level: str | None
    tags: list[str]
    cover_key: str | None
    cover_url: str | None
    cover_alt: str | None
    file_key: str | None
    file_name: str | None
    mime_type: str | None
    size_bytes: int | None
    external_url: str | None
    source: str | None
    licence: str | None
    status: str
    display_order: int
    open_count: int
    published_at: datetime | None
    updated_at: datetime
    problems: list[str]


class StorageOut(BaseModel):
    configured: bool
    cover_max_bytes: int
    file_max_bytes: int
    cover_types: list[str]
    file_types: list[str]


# --- helpers ---------------------------------------------------------------------


def _category_or_404(session: Session, category_id: UUID) -> ResourceCategory:
    category = session.get(ResourceCategory, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found.")
    return category


def _resource_or_404(session: Session, resource_id: UUID) -> Resource:
    resource = session.get(Resource, resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail="Resource not found.")
    return resource


def publish_problems(resource: Resource) -> list[str]:
    problems = []
    if not resource.cover_key:
        problems.append("Add a cover image.")
    if not (resource.cover_alt or "").strip():
        problems.append("Describe the cover image (alt text) for screen-reader users.")
    if resource.kind in ("link", "video"):
        if not resource.external_url:
            problems.append("Add the link.")
    elif not resource.file_key:
        problems.append("Upload the file.")
    if not (resource.licence or "").strip():
        problems.append("Record the licence (for example 'Original' or 'CC BY 4.0').")
    return problems


def _to_out(resource: Resource, category: ResourceCategory, bucket: r2.Bucket | None) -> AdminResourceOut:
    return AdminResourceOut(
        id=str(resource.id),
        category_id=str(resource.category_id),
        category_name=category.name,
        title=resource.title,
        description=resource.description,
        kind=resource.kind,
        module=resource.module,
        track=resource.track,
        level=resource.level,
        tags=list(resource.tags or []),
        cover_key=resource.cover_key,
        cover_url=bucket.download_url(resource.cover_key, expires=3600) if bucket and resource.cover_key else None,
        cover_alt=resource.cover_alt,
        file_key=resource.file_key,
        file_name=resource.file_name,
        mime_type=resource.mime_type,
        size_bytes=resource.size_bytes,
        external_url=resource.external_url,
        source=resource.source,
        licence=resource.licence,
        status=resource.status,
        display_order=resource.display_order,
        open_count=resource.open_count,
        published_at=resource.published_at,
        updated_at=resource.updated_at,
        problems=publish_problems(resource),
    )


def _apply_fields(session: Session, resource: Resource, body: ResourceFields) -> list[str]:
    """Copies fields onto a resource, verifying any newly uploaded object.
    Returns R2 keys that are no longer used (to delete after commit)."""

    data = body.model_dump(exclude_unset=True)
    stale: list[str] = []

    if "category_id" in data and data["category_id"] is not None:
        _category_or_404(session, data["category_id"])
        resource.category_id = data["category_id"]
    for field in ("title", "description", "kind", "module", "track", "level", "cover_alt", "external_url", "source", "licence", "display_order"):
        if field in data and (data[field] is not None or field in ("description", "level", "cover_alt", "external_url", "source", "licence")):
            setattr(resource, field, data[field])
    if "tags" in data and data["tags"] is not None:
        resource.tags = data["tags"]

    if "cover_key" in data:
        new_key = data["cover_key"]
        if new_key != resource.cover_key:
            if new_key is not None:
                _require_unused_key(session, resource, new_key)
                extension = new_key.rsplit(".", 1)[-1]
                r2.verify_object(r2.require_bucket(), new_key, "cover", COVER_EXT_TYPES.get(extension, ""))
            if resource.cover_key:
                stale.append(resource.cover_key)
            resource.cover_key = new_key

    if "file_key" in data:
        new_key = data["file_key"]
        if new_key != resource.file_key:
            if new_key is not None:
                _require_unused_key(session, resource, new_key)
                content_type = data.get("file_content_type") or ""
                allowed = KIND_TYPES.get(resource.kind, set())
                if content_type not in allowed:
                    raise HTTPException(status_code=422, detail=f"A {resource.kind} resource can't use a {content_type or 'file of unknown type'}.")
                resource.size_bytes = r2.verify_object(r2.require_bucket(), new_key, "file", content_type)
                resource.mime_type = content_type
                resource.file_name = data.get("file_name") or resource.file_name or new_key.rsplit("/", 1)[-1]
            else:
                resource.size_bytes = None
                resource.mime_type = None
                resource.file_name = None
            if resource.file_key:
                stale.append(resource.file_key)
            resource.file_key = new_key

    # Keep the shape consistent with the kind.
    if resource.kind in ("link", "video"):
        if resource.file_key:
            stale.append(resource.file_key)
            resource.file_key = resource.file_name = resource.mime_type = None
            resource.size_bytes = None
    else:
        resource.external_url = None
        if resource.file_key and resource.mime_type not in KIND_TYPES[resource.kind]:
            raise HTTPException(status_code=422, detail="The uploaded file doesn't match the resource type. Upload a new file.")

    if resource.status == "published":
        problems = publish_problems(resource)
        if problems:
            raise HTTPException(status_code=422, detail={"message": "A published resource must stay complete.", "problems": problems})
    resource.updated_at = _now()
    return stale


def _key_owners(session: Session, key: str, exclude_id: UUID | None = None):
    query = select(Resource.id).where(or_(Resource.cover_key == key, Resource.file_key == key))
    if exclude_id is not None:
        query = query.where(Resource.id != exclude_id)
    return session.exec(query).first()


def _require_unused_key(session: Session, resource: Resource, key: str) -> None:
    """Each uploaded object belongs to one resource: cleanup deletes an
    object when its resource drops it, which would break a second owner."""

    if _key_owners(session, key, exclude_id=resource.id) is not None:
        raise HTTPException(status_code=409, detail="That upload is already used by another resource. Upload the file again.")


def _delete_objects(session: Session, keys: list[str]) -> None:
    """Deletes R2 objects no resource references any more (after commit)."""

    bucket = r2.get_bucket()
    if bucket:
        for key in dict.fromkeys(keys):
            if _key_owners(session, key) is None:
                bucket.delete(key)


# --- categories ----------------------------------------------------------------------


def _category_out(session: Session, category: ResourceCategory) -> AdminCategoryOut:
    total = session.exec(select(func.count()).select_from(Resource).where(Resource.category_id == category.id)).one()
    published = session.exec(
        select(func.count()).select_from(Resource).where(Resource.category_id == category.id, Resource.status == "published")
    ).one()
    return AdminCategoryOut(
        id=str(category.id),
        name=category.name,
        slug=category.slug,
        description=category.description,
        display_order=category.display_order,
        is_visible=category.is_visible,
        resource_count=total,
        published_count=published,
    )


@router.get("/admin/resource-categories", response_model=list[AdminCategoryOut])
def admin_list_categories(_: str = Depends(require_admin), session: Session = Depends(get_session)) -> list[AdminCategoryOut]:
    categories = session.exec(select(ResourceCategory).order_by(ResourceCategory.display_order, ResourceCategory.name)).all()
    return [_category_out(session, c) for c in categories]


@router.post("/admin/resource-categories", response_model=AdminCategoryOut, status_code=201)
def admin_create_category(
    body: CategoryIn, request: Request, _: str = Depends(require_admin), session: Session = Depends(get_session)
) -> AdminCategoryOut:
    slug = body.slug or slugify(body.name)
    if not slug:
        raise HTTPException(status_code=422, detail="Choose a name with some letters or numbers.")
    if session.exec(select(ResourceCategory).where(ResourceCategory.slug == slug)).first():
        raise HTTPException(status_code=409, detail="A category with that name or slug already exists.")
    now = _now()
    category = ResourceCategory(
        id=uuid4(), name=body.name.strip(), slug=slug, description=body.description, display_order=body.display_order,
        is_visible=body.is_visible, created_at=now, updated_at=now,
    )
    session.add(category)
    session.commit()
    session.refresh(category)
    audit_note(request, category_id=str(category.id), slug=slug)
    return _category_out(session, category)


@router.patch("/admin/resource-categories/{category_id}", response_model=AdminCategoryOut)
def admin_update_category(
    category_id: UUID, body: CategoryPatch, request: Request, _: str = Depends(require_admin), session: Session = Depends(get_session)
) -> AdminCategoryOut:
    category = _category_or_404(session, category_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("slug") and data["slug"] != category.slug:
        if session.exec(select(ResourceCategory).where(ResourceCategory.slug == data["slug"])).first():
            raise HTTPException(status_code=409, detail="Another category already uses that slug.")
    for field, value in data.items():
        if value is not None or field == "description":
            setattr(category, field, value.strip() if field == "name" and value else value)
    category.updated_at = _now()
    session.add(category)
    session.commit()
    session.refresh(category)
    audit_note(request, category_id=str(category.id), changed=sorted(data))
    return _category_out(session, category)


@router.delete("/admin/resource-categories/{category_id}", status_code=204)
def admin_delete_category(
    category_id: UUID, request: Request, _: str = Depends(require_admin), session: Session = Depends(get_session)
) -> None:
    category = _category_or_404(session, category_id)
    used = session.exec(select(func.count()).select_from(Resource).where(Resource.category_id == category_id)).one()
    if used:
        raise HTTPException(
            status_code=409, detail=f"This category still has {used} resource(s). Move or delete them first, or hide the category."
        )
    session.delete(category)
    session.commit()
    audit_note(request, category_id=str(category_id), slug=category.slug)


# --- resources -----------------------------------------------------------------------


@router.get("/admin/resources/storage", response_model=StorageOut)
def admin_storage(_: str = Depends(require_admin)) -> StorageOut:
    return StorageOut(
        configured=r2.get_bucket() is not None,
        cover_max_bytes=r2.COVER_MAX_BYTES,
        file_max_bytes=r2.FILE_MAX_BYTES,
        cover_types=sorted(r2.COVER_TYPES),
        file_types=sorted(r2.FILE_TYPES),
    )


@router.post("/admin/resources/uploads", response_model=UploadOut, dependencies=[Depends(per_user(admin_bulk_limiter))])
def admin_create_upload(body: UploadIn, request: Request, _: str = Depends(require_admin)) -> UploadOut:
    types = r2.COVER_TYPES if body.purpose == "cover" else r2.FILE_TYPES
    limit = r2.COVER_MAX_BYTES if body.purpose == "cover" else r2.FILE_MAX_BYTES
    extension = types.get(body.content_type)
    if extension is None:
        allowed = "JPEG, PNG or WebP" if body.purpose == "cover" else "PDF, Word (.docx), PowerPoint (.pptx), JPEG, PNG or WebP"
        raise HTTPException(status_code=422, detail=f"That file type isn't allowed. Use {allowed}.")
    if body.size_bytes > limit:
        raise HTTPException(status_code=422, detail=f"That file is too large (the limit is {limit // (1024 * 1024)} MB).")
    bucket = r2.require_bucket()
    folder = "covers" if body.purpose == "cover" else "files"
    key = f"resources/{folder}/{uuid4()}.{extension}"
    audit_note(request, purpose=body.purpose, key=key, size_bytes=body.size_bytes)
    return UploadOut(
        key=key,
        upload_url=bucket.upload_url(key, body.content_type, body.size_bytes),
        headers={"Content-Type": body.content_type},
        expires_in=600,
    )


@router.get("/admin/resources", response_model=list[AdminResourceOut])
def admin_list_resources(
    status: str | None = Query(default=None, pattern="^(draft|published|archived)$"),
    category_id: UUID | None = None,
    q: str | None = Query(default=None, max_length=100),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[AdminResourceOut]:
    query = select(Resource, ResourceCategory).join(ResourceCategory, ResourceCategory.id == Resource.category_id)
    if status:
        query = query.where(Resource.status == status)
    if category_id:
        query = query.where(Resource.category_id == category_id)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        query = query.where(or_(func.lower(Resource.title).like(like), func.lower(func.coalesce(Resource.description, "")).like(like)))
    bucket = r2.get_bucket()
    rows = session.exec(query.order_by(ResourceCategory.display_order, Resource.display_order, Resource.updated_at.desc())).all()
    return [_to_out(res, cat, bucket) for res, cat in rows]


@router.get("/admin/resources/{resource_id}", response_model=AdminResourceOut)
def admin_get_resource(resource_id: UUID, _: str = Depends(require_admin), session: Session = Depends(get_session)) -> AdminResourceOut:
    resource = _resource_or_404(session, resource_id)
    return _to_out(resource, _category_or_404(session, resource.category_id), r2.get_bucket())


@router.post("/admin/resources", response_model=AdminResourceOut, status_code=201)
def admin_create_resource(
    body: ResourceCreate,
    request: Request,
    _: str = Depends(require_admin),
    admin_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> AdminResourceOut:
    category = _category_or_404(session, body.category_id)
    now = _now()
    resource = Resource(
        id=uuid4(), category_id=category.id, title=body.title.strip(), kind=body.kind, status="draft",
        created_by=UUID(admin_id), created_at=now, updated_at=now, tags=[],
    )
    _apply_fields(session, resource, body)
    session.add(resource)
    session.commit()
    session.refresh(resource)
    audit_note(request, resource_id=str(resource.id), title=resource.title)
    return _to_out(resource, category, r2.get_bucket())


@router.patch("/admin/resources/{resource_id}", response_model=AdminResourceOut)
def admin_update_resource(
    resource_id: UUID, body: ResourceFields, request: Request, _: str = Depends(require_admin), session: Session = Depends(get_session)
) -> AdminResourceOut:
    resource = _resource_or_404(session, resource_id)
    stale = _apply_fields(session, resource, body)
    session.add(resource)
    session.commit()
    session.refresh(resource)
    _delete_objects(session, stale)
    audit_note(request, resource_id=str(resource.id), changed=sorted(body.model_dump(exclude_unset=True)))
    return _to_out(resource, _category_or_404(session, resource.category_id), r2.get_bucket())


@router.post("/admin/resources/{resource_id}/status", response_model=AdminResourceOut)
def admin_set_status(
    resource_id: UUID, body: StatusIn, request: Request, _: str = Depends(require_admin), session: Session = Depends(get_session)
) -> AdminResourceOut:
    resource = _resource_or_404(session, resource_id)
    if body.status == "published":
        problems = publish_problems(resource)
        if problems:
            raise HTTPException(status_code=422, detail={"message": "This resource isn't ready to publish.", "problems": problems})
        if resource.published_at is None:
            resource.published_at = _now()
    resource.status = body.status
    resource.updated_at = _now()
    session.add(resource)
    session.commit()
    session.refresh(resource)
    audit_note(request, resource_id=str(resource.id), status=body.status)
    return _to_out(resource, _category_or_404(session, resource.category_id), r2.get_bucket())


@router.delete("/admin/resources/{resource_id}", status_code=204)
def admin_delete_resource(
    resource_id: UUID, request: Request, _: str = Depends(require_admin), session: Session = Depends(get_session)
) -> None:
    resource = _resource_or_404(session, resource_id)
    keys = [key for key in (resource.cover_key, resource.file_key) if key]
    title = resource.title
    session.delete(resource)
    session.commit()
    _delete_objects(session, keys)
    audit_note(request, resource_id=str(resource_id), title=title)

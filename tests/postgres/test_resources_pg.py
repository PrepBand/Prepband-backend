"""Resource library on real Postgres: full-text search (word forms, tags,
category names, typos when pg_trgm is installed), row-level security and
the database's own completeness rule."""

import uuid

import psycopg
import pytest

from app.services import r2

from ..conftest import as_user
from ..test_resources import PDF, PNG, FakeBucket
from .conftest import as_role, create_user, reset_role

pytestmark = pytest.mark.postgres


@pytest.fixture()
def bucket(monkeypatch):
    fake = FakeBucket()
    monkeypatch.setattr(r2, "get_bucket", lambda: fake)
    return fake


def _upload(client, admin, bucket, purpose, content_type, data):
    key = client.post(
        "/api/v1/admin/resources/uploads",
        json={"purpose": purpose, "file_name": "x", "content_type": content_type, "size_bytes": len(data)},
        headers=as_user(admin),
    ).json()["key"]
    bucket.objects[key] = data
    return key


def _publish(client, admin, bucket, category_slug, title, **extra):
    categories = {c["slug"]: c["id"] for c in client.get("/api/v1/admin/resource-categories", headers=as_user(admin)).json()}
    created = client.post(
        "/api/v1/admin/resources",
        json={
            "category_id": categories[category_slug], "title": title, "kind": "pdf",
            "cover_key": _upload(client, admin, bucket, "cover", "image/png", PNG), "cover_alt": "Cover",
            "file_key": _upload(client, admin, bucket, "file", "application/pdf", PDF), "file_content_type": "application/pdf",
            "file_name": "f.pdf", "licence": "Original", **extra,
        },
        headers=as_user(admin),
    )
    assert created.status_code == 201, created.text
    resource_id = created.json()["id"]
    assert client.post(f"/api/v1/admin/resources/{resource_id}/status", json={"status": "published"}, headers=as_user(admin)).status_code == 200
    return resource_id


def _seed_categories(pg):
    # TRUNCATE between tests removes the migration's starting categories.
    pg.execute(
        "insert into public.resource_categories (name, slug, display_order) values "
        "('Strategy guides', 'strategy-guides', 10), ('Vocabulary', 'vocabulary', 30) on conflict do nothing"
    )


def test_full_text_search_on_postgres(pg_client, pg, bucket):
    _seed_categories(pg)
    admin, student = create_user(pg, "Ada Admin", role="admin"), create_user(pg)
    _publish(pg_client, admin, bucket, "strategy-guides", "Strategies for True/False/Not Given", tags=["reading", "skimming"])
    _publish(pg_client, admin, bucket, "vocabulary", "Environment word list", description="Collocations about pollution and climate.")

    def titles(q):
        page = pg_client.get(f"/api/v1/resources?q={q}", headers=as_user(student))
        assert page.status_code == 200, page.text
        return [item["title"] for item in page.json()["items"]]

    assert titles("strategy") == ["Strategies for True/False/Not Given"]  # word forms
    assert titles("skimming") == ["Strategies for True/False/Not Given"]  # tags
    assert titles("climate") == ["Environment word list"]  # description
    assert titles("vocabulary") == ["Environment word list"]  # category name
    assert titles("cooking") == []


def test_typos_still_find_titles_when_pg_trgm_is_installed(pg_client, pg, bucket):
    if not pg.execute("select exists(select 1 from pg_extension where extname = 'pg_trgm')").fetchone()[0]:
        pytest.skip("pg_trgm isn't available on this Postgres (it is on Supabase and in CI).")
    _seed_categories(pg)
    admin, student = create_user(pg, "Ada Admin", role="admin"), create_user(pg)
    _publish(pg_client, admin, bucket, "vocabulary", "Environment word list")
    items = pg_client.get("/api/v1/resources?q=enviroment", headers=as_user(student)).json()["items"]
    assert [i["title"] for i in items] == ["Environment word list"]


def test_renaming_a_category_reindexes_its_resources(pg_client, pg, bucket):
    _seed_categories(pg)
    admin, student = create_user(pg, "Ada Admin", role="admin"), create_user(pg)
    _publish(pg_client, admin, bucket, "vocabulary", "Environment word list")
    pg.execute("update public.resource_categories set name = 'Lexicon' where slug = 'vocabulary'")
    assert [i["title"] for i in pg_client.get("/api/v1/resources?q=lexicon", headers=as_user(student)).json()["items"]] == [
        "Environment word list"
    ]


def test_browsers_read_only_published_rows_and_write_nothing(pg_client, pg, bucket):
    _seed_categories(pg)
    admin, student = create_user(pg, "Ada Admin", role="admin"), create_user(pg)
    _publish(pg_client, admin, bucket, "vocabulary", "Published list")
    category_id = pg.execute("select id from public.resource_categories where slug = 'vocabulary'").fetchone()[0]
    pg.execute(
        "insert into public.resources (category_id, title, kind) values (%s, 'Draft list', 'link')", (category_id,)
    )

    as_role(pg, "authenticated", student)
    try:
        assert pg.execute("select title from public.resources").fetchall() == [("Published list",)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pg.execute("update public.resources set title = 'Hacked'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pg.execute("insert into public.resource_categories (name, slug) values ('Mine', 'mine')")
    finally:
        reset_role(pg)

    as_role(pg, "anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pg.execute("select count(*) from public.resources")
    finally:
        reset_role(pg)


def test_the_database_refuses_an_incomplete_published_resource(pg):
    _seed_categories(pg)
    category_id = pg.execute("select id from public.resource_categories where slug = 'vocabulary'").fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            "insert into public.resources (id, category_id, title, kind, status) values (%s, %s, 'No cover', 'link', 'published')",
            (str(uuid.uuid4()), category_id),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            "insert into public.resources (category_id, title, kind, external_url) values (%s, 'Insecure', 'link', 'http://x.org')",
            (category_id,),
        )



def test_a_published_link_can_become_a_pdf_in_one_edit(pg_client, pg, bucket):
    """PR #2 review: the new upload-ownership query autoflushed the half-edited
    row (kind pdf, no file yet), which resources_published_complete refused."""

    _seed_categories(pg)
    admin = create_user(pg, "Ada Admin", role="admin")
    resource_id = _publish(
        pg_client, admin, bucket, "strategy-guides", "Band descriptors",
        kind="link", file_key=None, file_content_type=None, file_name=None, external_url="https://example.org/descriptors",
    )
    file_key = _upload(pg_client, admin, bucket, "file", "application/pdf", PDF)
    changed = pg_client.patch(
        f"/api/v1/admin/resources/{resource_id}",
        json={"kind": "pdf", "file_key": file_key, "file_content_type": "application/pdf", "file_name": "descriptors.pdf"},
        headers=as_user(admin),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["kind"] == "pdf" and changed.json()["file_key"] == file_key

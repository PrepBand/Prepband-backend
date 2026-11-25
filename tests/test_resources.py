"""Resource library: admin categories and resources, private R2 uploads
(signed, type-checked), publishing rules, and what students can see,
search and open."""

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services import r2

from .conftest import as_user, make_user

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


class FakeBucket(r2.Bucket):
    """Stands in for R2: signs URLs for real (same code), stores objects in
    memory instead of over the network."""

    def __init__(self):
        super().__init__("acct123", "AKIDTEST", "secret/test", "prepband-resources")
        object.__setattr__(self, "objects", {})
        object.__setattr__(self, "deleted", [])

    def read_head(self, key, length=16):
        if key not in self.objects:
            raise r2.HTTPException(status_code=422, detail="The uploaded file wasn't found. Upload it again.")
        data = self.objects[key]
        return data[:length], len(data)

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


@pytest.fixture()
def bucket(monkeypatch):
    fake = FakeBucket()
    monkeypatch.setattr(r2, "get_bucket", lambda: fake)
    return fake


def _admin(session):
    return make_user(session, "Ada Admin", role="admin")


def _category(client, admin, name="Strategy guides", **extra):
    response = client.post("/api/v1/admin/resource-categories", json={"name": name, **extra}, headers=as_user(admin))
    assert response.status_code == 201, response.text
    return response.json()


def _upload(client, admin, bucket, purpose, content_type, data):
    response = client.post(
        "/api/v1/admin/resources/uploads",
        json={"purpose": purpose, "file_name": "x", "content_type": content_type, "size_bytes": len(data)},
        headers=as_user(admin),
    )
    assert response.status_code == 200, response.text
    key = response.json()["key"]
    bucket.objects[key] = data  # what the browser's PUT would store
    return key


def _published_pdf(client, admin, bucket, category_id, title="Reading strategy guide", **extra):
    cover = _upload(client, admin, bucket, "cover", "image/png", PNG)
    file_key = _upload(client, admin, bucket, "file", "application/pdf", PDF)
    created = client.post(
        "/api/v1/admin/resources",
        json={
            "category_id": category_id, "title": title, "kind": "pdf", "cover_key": cover, "cover_alt": "A stack of books",
            "file_key": file_key, "file_name": "guide.pdf", "file_content_type": "application/pdf", "licence": "Original",
            **extra,
        },
        headers=as_user(admin),
    )
    assert created.status_code == 201, created.text
    published = client.post(f"/api/v1/admin/resources/{created.json()['id']}/status", json={"status": "published"}, headers=as_user(admin))
    assert published.status_code == 200, published.text
    return published.json()


# --- signing -----------------------------------------------------------------------


def test_signatures_match_the_aws_published_example():
    url = r2.presign(
        method="GET", host="examplebucket.s3.amazonaws.com", path="/test.txt", access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", region="us-east-1", expires=86400,
        now=datetime(2013, 5, 24, tzinfo=timezone.utc),
    )
    assert url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")


def test_upload_urls_are_bound_to_the_declared_type_and_size(client, session, bucket):
    admin = _admin(session)
    response = client.post(
        "/api/v1/admin/resources/uploads",
        json={"purpose": "file", "file_name": "g.pdf", "content_type": "application/pdf", "size_bytes": 1234},
        headers=as_user(admin),
    ).json()
    assert response["key"].startswith("resources/files/") and response["key"].endswith(".pdf")
    query = parse_qs(urlsplit(response["upload_url"]).query)
    assert urlsplit(response["upload_url"]).hostname == "acct123.r2.cloudflarestorage.com"
    assert query["X-Amz-SignedHeaders"] == ["content-length;content-type;host"]
    assert query["X-Amz-Expires"] == ["600"]


def test_uploads_refuse_wrong_types_big_files_and_missing_storage(client, session, bucket, monkeypatch):
    admin = _admin(session)
    def ask(**body):
        return client.post("/api/v1/admin/resources/uploads", json={"file_name": "x", **body}, headers=as_user(admin))

    assert ask(purpose="cover", content_type="image/svg+xml", size_bytes=10).status_code == 422
    assert ask(purpose="file", content_type="application/x-msdownload", size_bytes=10).status_code == 422
    assert ask(purpose="cover", content_type="image/png", size_bytes=r2.COVER_MAX_BYTES + 1).status_code == 422
    assert ask(purpose="file", content_type="application/pdf", size_bytes=r2.FILE_MAX_BYTES + 1).status_code == 422
    monkeypatch.setattr(r2, "get_bucket", lambda: None)
    assert ask(purpose="file", content_type="application/pdf", size_bytes=10).status_code == 503


def test_a_file_that_isnt_what_it_claims_is_deleted_and_refused(client, session, bucket):
    admin = _admin(session)
    category = _category(client, admin)
    key = _upload(client, admin, bucket, "file", "application/pdf", b"MZ\x90\x00 not a pdf at all")
    response = client.post(
        "/api/v1/admin/resources",
        json={"category_id": category["id"], "title": "Sneaky", "kind": "pdf", "file_key": key, "file_content_type": "application/pdf"},
        headers=as_user(admin),
    )
    assert response.status_code == 422
    assert key in bucket.deleted


def test_a_key_outside_the_resource_folders_is_refused(client, session, bucket):
    admin = _admin(session)
    category = _category(client, admin)
    bucket.objects["avatars/someone/me.png"] = PNG
    response = client.post(
        "/api/v1/admin/resources",
        json={"category_id": category["id"], "title": "Steal avatar", "kind": "image", "cover_key": "avatars/someone/me.png"},
        headers=as_user(admin),
    )
    assert response.status_code == 422


# --- categories --------------------------------------------------------------------------


def test_admins_manage_categories(client, session, bucket):
    admin, student = _admin(session), make_user(session)
    created = _category(client, admin, "Exam-day Tips!")
    assert created["slug"] == "exam-day-tips"
    assert client.post("/api/v1/admin/resource-categories", json={"name": "Exam day tips"}, headers=as_user(admin)).status_code == 409
    renamed = client.patch(f"/api/v1/admin/resource-categories/{created['id']}", json={"name": "Exam day", "display_order": 5}, headers=as_user(admin))
    assert renamed.json()["name"] == "Exam day" and renamed.json()["display_order"] == 5
    # Students can't manage them.
    assert client.post("/api/v1/admin/resource-categories", json={"name": "Nope"}, headers=as_user(student)).status_code == 403


def test_a_category_with_resources_cant_be_deleted(client, session, bucket):
    admin = _admin(session)
    category = _category(client, admin)
    _published_pdf(client, admin, bucket, category["id"])
    response = client.delete(f"/api/v1/admin/resource-categories/{category['id']}", headers=as_user(admin))
    assert response.status_code == 409 and "Move or delete" in response.json()["error"]["message"]
    empty = _category(client, admin, "Empty one")
    assert client.delete(f"/api/v1/admin/resource-categories/{empty['id']}", headers=as_user(admin)).status_code == 204


# --- publishing --------------------------------------------------------------------------


def test_only_complete_resources_can_be_published(client, session, bucket):
    admin = _admin(session)
    category = _category(client, admin)
    draft = client.post(
        "/api/v1/admin/resources", json={"category_id": category["id"], "title": "Half done", "kind": "link"}, headers=as_user(admin)
    ).json()
    assert set(draft["problems"]) >= {"Add a cover image.", "Add the link."}
    refused = client.post(f"/api/v1/admin/resources/{draft['id']}/status", json={"status": "published"}, headers=as_user(admin))
    assert refused.status_code == 422
    assert "Add the link." in [p["msg"] for p in refused.json()["error"]["details"]]


def test_links_must_be_https_without_credentials(client, session, bucket):
    admin = _admin(session)
    category = _category(client, admin)
    for url in ("http://example.com/guide", "https://user:pass@example.com/", "javascript:alert(1)", "https://localhost"):
        response = client.post(
            "/api/v1/admin/resources",
            json={"category_id": category["id"], "title": "Bad link", "kind": "link", "external_url": url},
            headers=as_user(admin),
        )
        assert response.status_code == 422, url


def test_a_pdf_resource_cant_carry_a_word_file(client, session, bucket):
    admin = _admin(session)
    category = _category(client, admin)
    key = _upload(client, admin, bucket, "file", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK\x03\x04rest")
    response = client.post(
        "/api/v1/admin/resources",
        json={
            "category_id": category["id"], "title": "Mismatch", "kind": "pdf", "file_key": key,
            "file_content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        },
        headers=as_user(admin),
    )
    assert response.status_code == 422


def test_replacing_and_deleting_files_cleans_up_storage(client, session, bucket):
    admin = _admin(session)
    category = _category(client, admin)
    resource = _published_pdf(client, admin, bucket, category["id"])
    old_cover = resource["cover_key"]
    new_cover = _upload(client, admin, bucket, "cover", "image/png", PNG)
    updated = client.patch(f"/api/v1/admin/resources/{resource['id']}", json={"cover_key": new_cover}, headers=as_user(admin))
    assert updated.status_code == 200 and updated.json()["cover_key"] == new_cover
    assert old_cover in bucket.deleted
    # A published resource can't be left incomplete.
    assert client.patch(f"/api/v1/admin/resources/{resource['id']}", json={"cover_alt": ""}, headers=as_user(admin)).status_code == 422

    assert client.delete(f"/api/v1/admin/resources/{resource['id']}", headers=as_user(admin)).status_code == 204
    assert new_cover in bucket.deleted and resource["file_key"] in bucket.deleted


# --- students ------------------------------------------------------------------------------


def test_students_see_only_published_resources_in_visible_categories(client, session, bucket):
    admin, student = _admin(session), make_user(session)
    guides = _category(client, admin, "Strategy guides")
    hidden = _category(client, admin, "Coming soon")
    _published_pdf(client, admin, bucket, guides["id"], title="Reading strategy guide")
    _published_pdf(client, admin, bucket, hidden["id"], title="Secret plan")
    client.post("/api/v1/admin/resources", json={"category_id": guides["id"], "title": "Draft only", "kind": "link"}, headers=as_user(admin))
    client.patch(f"/api/v1/admin/resource-categories/{hidden['id']}", json={"is_visible": False}, headers=as_user(admin))

    page = client.get("/api/v1/resources", headers=as_user(student)).json()
    assert [item["title"] for item in page["items"]] == ["Reading strategy guide"]
    card = page["items"][0]
    assert card["category_slug"] == "strategy-guides" and card["cover_alt"] == "A stack of books"
    assert "X-Amz-Signature=" in card["cover_url"] and "X-Amz-Expires=3600" in card["cover_url"]
    assert "file_key" not in card  # storage keys never reach students

    categories = client.get("/api/v1/resource-categories", headers=as_user(student)).json()
    assert [(c["slug"], c["resource_count"]) for c in categories] == [("strategy-guides", 1)]
    assert client.get("/api/v1/resources").status_code == 401


def test_search_and_filters(client, session, bucket):
    admin, student = _admin(session), make_user(session)
    guides = _category(client, admin, "Strategy guides")
    vocab = _category(client, admin, "Vocabulary")
    _published_pdf(client, admin, bucket, guides["id"], title="Reading strategy guide", module="reading", tags=["skimming"])
    _published_pdf(client, admin, bucket, vocab["id"], title="Environment word list", module="writing", track="academic")
    _published_pdf(client, admin, bucket, vocab["id"], title="Travel word list", module="speaking", track="general")

    def titles(query):
        return sorted(i["title"] for i in client.get(f"/api/v1/resources?{query}", headers=as_user(student)).json()["items"])

    assert titles("q=word") == ["Environment word list", "Travel word list"]
    assert titles("q=skimming") == ["Reading strategy guide"]  # tags are searched
    assert titles("q=vocabulary") == ["Environment word list", "Travel word list"]  # so is the category
    assert titles("category=vocabulary&module=writing") == ["Environment word list"]
    assert titles("track=academic") == ["Environment word list", "Reading strategy guide"]  # "both" suits either
    assert titles("kind=link") == []
    assert client.get("/api/v1/resources?q=nothingmatches", headers=as_user(student)).json()["total"] == 0


def test_opening_a_file_gives_a_short_signed_link_and_counts_it(client, session, bucket):
    admin, student = _admin(session), make_user(session)
    category = _category(client, admin)
    resource = _published_pdf(client, admin, bucket, category["id"])

    opened = client.post(f"/api/v1/resources/{resource['id']}/open", headers=as_user(student)).json()
    query = parse_qs(urlsplit(opened["url"]).query)
    assert opened["expires_in"] == 300 and query["X-Amz-Expires"] == ["300"]
    assert query["response-content-disposition"] == ['inline; filename="guide.pdf"']
    admin_view = client.get(f"/api/v1/admin/resources/{resource['id']}", headers=as_user(admin)).json()
    assert admin_view["open_count"] == 1


def test_links_open_their_address_and_drafts_cant_be_opened(client, session, bucket):
    admin, student = _admin(session), make_user(session)
    category = _category(client, admin)
    cover = _upload(client, admin, bucket, "cover", "image/png", PNG)
    link = client.post(
        "/api/v1/admin/resources",
        json={
            "category_id": category["id"], "title": "Official band descriptors", "kind": "link",
            "external_url": "https://example.org/descriptors", "cover_key": cover, "cover_alt": "Chart", "licence": "Linked, not copied",
        },
        headers=as_user(admin),
    ).json()
    assert client.post(f"/api/v1/resources/{link['id']}/open", headers=as_user(student)).status_code == 404  # still a draft
    client.post(f"/api/v1/admin/resources/{link['id']}/status", json={"status": "published"}, headers=as_user(admin))
    opened = client.post(f"/api/v1/resources/{link['id']}/open", headers=as_user(student)).json()
    assert opened == {"url": "https://example.org/descriptors", "expires_in": None}

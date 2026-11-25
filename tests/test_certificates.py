"""Phase 4 - certificates, PDF and verification (FR-CERT-*; acceptance
scenarios 3 and 10)."""

import re

from app.services.certificate_pdf import UNOFFICIAL_LABEL
from app.services.mock_flow import generate_certificate_code

from .conftest import as_user, make_user, publish_full_mock
from .test_full_mock import _finish_module


def _completed_certificate(client, session, name="Amina Khan"):
    user = make_user(session, name)
    publish_full_mock(session)
    mock = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    for module in ("listening", "reading", "writing", "speaking"):
        _finish_module(client, user, mock["id"], module)
    certificate = client.get(f"/api/v1/mocks/{mock['id']}", headers=as_user(user)).json()["certificate"]
    return user, mock, certificate


def test_certificate_code_format_and_uniqueness():
    codes = {generate_certificate_code() for _ in range(2000)}
    assert len(codes) == 2000
    assert all(re.fullmatch(r"PB(-[A-Z0-9]{4}){4}", code) for code in codes)
    assert not any(ch in code for code in codes for ch in "IO01L")


def test_no_certificate_before_completion(client, session):
    user = make_user(session)
    publish_full_mock(session)
    mock = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    for module in ("listening", "reading", "writing"):
        _finish_module(client, user, mock["id"], module)
    progress = client.get(f"/api/v1/mocks/{mock['id']}", headers=as_user(user)).json()
    assert progress["certificate"] is None
    assert client.get("/api/v1/certificates", headers=as_user(user)).json() == []


def test_certificate_list_and_detail(client, session):
    user, mock, certificate = _completed_certificate(client, session)
    listed = client.get("/api/v1/certificates", headers=as_user(user)).json()
    assert len(listed) == 1
    assert listed[0]["code"] == certificate["code"]
    assert listed[0]["track"] == "academic"
    assert listed[0]["verification_url"] is None

    detail = client.get(f"/api/v1/certificates/{certificate['id']}", headers=as_user(user)).json()
    assert detail["holder_name"] == "Amina Khan"
    assert [m["module"] for m in detail["modules"]] == ["listening", "reading", "writing", "speaking"]
    assert detail["overall_band_note"].startswith("Overall band: Not available.")


def test_certificate_pdf(client, session):
    user, mock, certificate = _completed_certificate(client, session, name="Zoë O'Brien")
    response = client.get(f"/api/v1/certificates/{certificate['id']}/pdf", headers=as_user(user))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert certificate["code"] in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    pdf = response.content
    assert pdf.startswith(b"%PDF") and len(pdf) > 5000
    # Document metadata carries the code and the unofficial label.
    assert certificate["code"].encode() in pdf
    assert b"Prepband practice completion certificate" in pdf


def test_certificate_pdf_rate_limit(client, session):
    user, _, certificate = _completed_certificate(client, session)
    statuses = [client.get(f"/api/v1/certificates/{certificate['id']}/pdf", headers=as_user(user)).status_code for _ in range(11)]
    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


def test_other_students_cannot_access_a_certificate(client, session):
    _, _, certificate = _completed_certificate(client, session)
    intruder = make_user(session, "Intruder")
    for method, path, kwargs in [
        ("get", f"/api/v1/certificates/{certificate['id']}", {}),
        ("get", f"/api/v1/certificates/{certificate['id']}/pdf", {}),
        ("patch", f"/api/v1/certificates/{certificate['id']}", {"json": {"public_verification": True}}),
    ]:
        assert getattr(client, method)(path, headers=as_user(intruder), **kwargs).status_code == 404
    assert client.get("/api/v1/certificates", headers=as_user(intruder)).json() == []


def test_certificate_cannot_be_forged_through_the_api(client, session):
    user, _, certificate = _completed_certificate(client, session)
    # Only the consent flag is accepted; scores, code and dates are ignored.
    response = client.patch(
        f"/api/v1/certificates/{certificate['id']}",
        json={"public_verification": False, "code": "PB-AAAA-AAAA-AAAA-AAAA", "issued_at": "2020-01-01T00:00:00Z"},
        headers=as_user(user),
    )
    assert response.status_code == 200
    assert response.json()["code"] == certificate["code"]
    assert response.json()["issued_at"] == certificate["issued_at"]
    assert client.patch(f"/api/v1/certificates/{certificate['id']}", json={}, headers=as_user(user)).status_code == 422


def test_public_verification_requires_consent(client, session):
    user, _, certificate = _completed_certificate(client, session)
    code = certificate["code"]

    # Private by default: indistinguishable from a code that doesn't exist.
    private = client.get(f"/api/v1/verify/{code}")
    missing = client.get("/api/v1/verify/PB-ZZZZ-ZZZZ-ZZZZ-ZZZZ")
    assert private.status_code == missing.status_code == 404
    assert private.json()["error"]["message"] == missing.json()["error"]["message"]

    updated = client.patch(f"/api/v1/certificates/{certificate['id']}", json={"public_verification": True}, headers=as_user(user)).json()
    assert updated["verification_url"].endswith(f"/verify/{code}")

    public = client.get(f"/api/v1/verify/{code.lower()}")
    assert public.status_code == 200
    body = public.json()
    assert set(body) == {"code", "status", "issued_on", "statement"}
    assert body["status"] == "valid" and body["code"] == code
    assert "Amina" not in public.text  # FR-CERT-06: no name, no scores

    # The PDF now carries a verification link.
    pdf = client.get(f"/api/v1/certificates/{certificate['id']}/pdf", headers=as_user(user)).content
    assert len(pdf) > 5000

    client.patch(f"/api/v1/certificates/{certificate['id']}", json={"public_verification": False}, headers=as_user(user))
    assert client.get(f"/api/v1/verify/{code}").status_code == 404


def test_verification_is_rate_limited(client, session):
    statuses = [client.get("/api/v1/verify/PB-ZZZZ-ZZZZ-ZZZZ-ZZZZ").status_code for _ in range(31)]
    assert statuses[:30] == [404] * 30
    assert statuses[30] == 429


def test_admin_revocation(client, session):
    user, _, certificate = _completed_certificate(client, session)
    admin = make_user(session, "Admin", role="admin")
    client.patch(f"/api/v1/certificates/{certificate['id']}", json={"public_verification": True}, headers=as_user(user))

    assert client.post(f"/api/v1/admin/certificates/{certificate['id']}/revoke", json={"reason": "Shared account"}, headers=as_user(user)).status_code == 403
    assert client.get("/api/v1/admin/certificates", headers=as_user(user)).status_code == 403

    listed = client.get("/api/v1/admin/certificates", headers=as_user(admin)).json()
    assert listed[0]["holder_name"] == "Amina Khan"

    revoked = client.post(f"/api/v1/admin/certificates/{certificate['id']}/revoke", json={"reason": "Shared account"}, headers=as_user(admin))
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert revoked.json()["verification_url"] is None

    verify = client.get(f"/api/v1/verify/{certificate['code']}").json()
    assert verify["status"] == "revoked"

    # Revoked certificates still download, clearly marked, and can't be re-published.
    assert client.get(f"/api/v1/certificates/{certificate['id']}/pdf", headers=as_user(user)).status_code == 200
    assert client.patch(f"/api/v1/certificates/{certificate['id']}", json={"public_verification": True}, headers=as_user(user)).status_code == 409


def test_unofficial_label_wording():
    assert UNOFFICIAL_LABEL == "UNOFFICIAL IELTS-STYLE PRACTICE COMPLETION CERTIFICATE — NOT AN OFFICIAL IELTS RESULT"

"""Phase 6 - admin audit trail, bulk editing, CSV import/export, question
analytics, rate/size limits, security headers and negative paths
(FR-ADMIN-08, spec section 9; acceptance scenarios 3, 4, 11, 12, 17, 19)."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlmodel import select

from app import models
from app.models import AdminAuditLog, Section

from .conftest import as_user, make_user, publish_test


def _admin(session):
    return make_user(session, "Ada Admin", role="admin")


def _draft_reading_test(client, admin):
    test = client.post(
        "/api/v1/admin/tests",
        json={"title": "CSV reading", "module": "reading", "track": "academic", "kind": "practice", "duration_minutes": 20},
        headers=as_user(admin),
    )
    assert test.status_code == 201, test.text
    test_id = test.json()["id"]
    section = client.post(f"/api/v1/admin/tests/{test_id}/sections", json={"order_index": 1, "module": "reading"}, headers=as_user(admin))
    assert section.status_code == 201, section.text
    return test_id, section.json()["id"]


CSV = (
    "display_order,type,prompt,marks,options,correct_answers,accepted_aliases,rationale\n"
    '1,short_answer,Which city?,1,,London,"the city of london",Paragraph B.\n'
    "2,multiple_choice,Pick one,1,Red|Green|Blue,Green,,\n"
    "3,true_false_not_given,The sky is green.,1,,False,,\n"
    '4,matching,Match them,2,"{""prompts"":[""A"",""B""],""choices"":[""x"",""y""]}","{""0"":""x"",""1"":""y""}",,\n'
)


# --- audit trail ---------------------------------------------------------


def test_admin_writes_are_audited_with_the_verified_actor(client, session):
    admin = _admin(session)
    test_id, _section = _draft_reading_test(client, admin)

    entries = session.exec(select(AdminAuditLog).order_by(AdminAuditLog.created_at)).all()
    actions = [e.action for e in entries]
    assert "POST /admin/tests" in actions
    assert "POST /admin/tests/{test_id}/sections" in actions
    create = next(e for e in entries if e.action == "POST /admin/tests")
    assert str(create.actor_id) == admin and create.actor_role == "admin" and create.outcome == "success"
    assert create.details["created_test_id"] == test_id
    section_entry = next(e for e in entries if e.action.endswith("/sections"))
    assert section_entry.entity_type == "test" and section_entry.entity_id == test_id


def test_plain_reads_are_not_audited_but_exports_are(client, session):
    admin = _admin(session)
    test_id, _ = _draft_reading_test(client, admin)
    before = len(session.exec(select(AdminAuditLog)).all())
    assert client.get("/api/v1/admin/tests", headers=as_user(admin)).status_code == 200
    assert len(session.exec(select(AdminAuditLog)).all()) == before
    assert client.get(f"/api/v1/admin/tests/{test_id}/export", headers=as_user(admin)).status_code == 200
    session.expire_all()
    assert session.exec(select(AdminAuditLog).where(AdminAuditLog.action == "GET /admin/tests/{test_id}/export")).first()


def test_refused_admin_access_is_logged_as_denied(client, session):
    student = make_user(session)
    # Scenario 4: a forged "role" anywhere in the request changes nothing.
    response = client.post(
        "/api/v1/admin/tests",
        json={"title": "x", "module": "reading", "track": "academic", "role": "admin"},
        headers={**as_user(student), "X-Role": "admin"},
    )
    assert response.status_code == 403
    entry = session.exec(select(AdminAuditLog).where(AdminAuditLog.outcome == "denied")).one()
    assert str(entry.actor_id) == student and entry.actor_role == "student" and entry.status_code == 403


def test_unauthenticated_admin_calls_are_not_logged(client, session):
    assert client.post("/api/v1/admin/tests", json={}).status_code == 401
    assert session.exec(select(AdminAuditLog)).all() == []


def test_failed_admin_write_is_logged_as_failed(client, session):
    admin = _admin(session)
    test_id, _ = _draft_reading_test(client, admin)
    # An empty section can't be published.
    assert client.post(f"/api/v1/admin/tests/{test_id}/publish", headers=as_user(admin)).status_code == 422
    entry = session.exec(select(AdminAuditLog).where(AdminAuditLog.action == "POST /admin/tests/{test_id}/publish")).one()
    assert entry.outcome == "failed" and entry.status_code == 422


def test_audit_listing_is_admin_only_and_filterable(client, session):
    admin, student = _admin(session), make_user(session)
    _draft_reading_test(client, admin)
    client.get("/api/v1/admin/users", headers=as_user(student))  # denied

    assert client.get("/api/v1/admin/audit", headers=as_user(student)).status_code == 403
    page = client.get("/api/v1/admin/audit", headers=as_user(admin)).json()
    assert page["total"] >= 3
    assert page["items"][0]["created_at"] >= page["items"][-1]["created_at"]
    denied = client.get("/api/v1/admin/audit?outcome=denied", headers=as_user(admin)).json()
    assert {i["outcome"] for i in denied["items"]} == {"denied"}
    assert denied["items"][0]["actor_email"].endswith("@example.test")


# --- bulk editing ----------------------------------------------------------


def test_bulk_archive_restore_and_per_item_errors(client, session):
    admin = _admin(session)
    a = publish_test(session, "reading", "academic", kind="practice")
    b = publish_test(session, "listening", "both", kind="practice")
    draft_id, _ = _draft_reading_test(client, admin)

    result = client.post(
        "/api/v1/admin/tests/bulk",
        json={"action": "archive", "test_ids": [str(a.id), str(b.id), draft_id]},
        headers=as_user(admin),
    ).json()
    assert result["succeeded"] == 2 and result["failed"] == 1
    failed = next(r for r in result["results"] if not r["ok"])
    assert failed["id"] == draft_id and "published" in failed["error"]
    session.expire_all()
    assert session.get(models.Test, a.id).status == "archived"

    restored = client.post(
        "/api/v1/admin/tests/bulk", json={"action": "restore", "test_ids": [str(a.id)]}, headers=as_user(admin)
    ).json()
    assert restored["succeeded"] == 1


def test_bulk_publish_reports_every_problem(client, session):
    admin = _admin(session)
    draft_id, _ = _draft_reading_test(client, admin)
    result = client.post("/api/v1/admin/tests/bulk", json={"action": "publish", "test_ids": [draft_id]}, headers=as_user(admin)).json()
    assert result["failed"] == 1 and result["results"][0]["problems"]


def test_bulk_is_admin_only(client, session):
    student = make_user(session)
    response = client.post("/api/v1/admin/tests/bulk", json={"action": "delete", "test_ids": [str(uuid4())]}, headers=as_user(student))
    assert response.status_code == 403


def test_bulk_question_marks_delete_and_renumber(client, session):
    admin = _admin(session)
    _, section_id = _draft_reading_test(client, admin)
    client.post(f"/api/v1/admin/sections/{section_id}/questions/import-csv", json={"csv": CSV}, headers=as_user(admin))
    questions = client.get(f"/api/v1/admin/sections/{section_id}/questions", headers=as_user(admin)).json()
    ids = [q["id"] for q in questions]

    url = f"/api/v1/admin/sections/{section_id}/questions/bulk"
    assert client.patch(url, json={"action": "set_marks", "question_ids": ids[:2], "marks": 2}, headers=as_user(admin)).status_code == 200
    assert client.patch(url, json={"action": "delete", "question_ids": [ids[0]]}, headers=as_user(admin)).status_code == 200
    assert client.patch(url, json={"action": "renumber"}, headers=as_user(admin)).json()["updated"] == 3
    after = client.get(f"/api/v1/admin/sections/{section_id}/questions", headers=as_user(admin)).json()
    assert [q["display_order"] for q in after] == [1, 2, 3]
    assert after[0]["marks"] == 2
    # A question from another section is refused.
    assert client.patch(url, json={"action": "delete", "question_ids": [str(uuid4())]}, headers=as_user(admin)).status_code == 422


def test_bulk_question_edit_refused_on_published_version(client, session):
    admin = _admin(session)
    test = publish_test(session, "reading", "academic")
    section = session.exec(select(Section).where(Section.test_version_id == test.current_version_id)).one()
    response = client.patch(f"/api/v1/admin/sections/{section.id}/questions/bulk", json={"action": "renumber"}, headers=as_user(admin))
    assert response.status_code == 409


# --- CSV import / export -------------------------------------------------


def test_csv_import_then_export_round_trip(client, session):
    admin = _admin(session)
    test_id, section_id = _draft_reading_test(client, admin)
    imported = client.post(f"/api/v1/admin/sections/{section_id}/questions/import-csv", json={"csv": CSV}, headers=as_user(admin))
    assert imported.status_code == 200, imported.text
    assert imported.json()["imported"] == 4

    content = client.get(f"/api/v1/admin/tests/{test_id}/content", headers=as_user(admin)).json()
    keys = {k["question_id"]: k for k in content["sections"][0]["answer_keys"]}
    by_type = {q["type"]: q for q in content["sections"][0]["questions"]}
    assert keys[by_type["short_answer"]["id"]]["accepted_aliases"] == ["the city of london"]
    assert keys[by_type["multiple_choice"]["id"]]["correct_answers"] == ["Green"]
    assert keys[by_type["true_false_not_given"]["id"]]["correct_answers"] == ["False"]
    assert keys[by_type["matching"]["id"]]["correct_answers"] == {"0": "x", "1": "y"}

    exported = client.get(f"/api/v1/admin/sections/{section_id}/questions.csv", headers=as_user(admin))
    assert exported.status_code == 200 and exported.headers["content-type"].startswith("text/csv")
    assert "Red|Green|Blue" in exported.text

    # Re-importing the export in replace mode gives the same four questions.
    again = client.post(
        f"/api/v1/admin/sections/{section_id}/questions/import-csv",
        json={"csv": exported.text, "mode": "replace"},
        headers=as_user(admin),
    )
    assert again.status_code == 200 and again.json()["imported"] == 4
    assert len(client.get(f"/api/v1/admin/sections/{section_id}/questions", headers=as_user(admin)).json()) == 4


def test_csv_import_is_all_or_nothing_with_every_problem_listed(client, session):
    admin = _admin(session)
    _, section_id = _draft_reading_test(client, admin)
    bad = (
        "type,prompt,options,correct_answers\n"
        "short_answer,Fine question,,yes\n"
        "multiple_choice,Bad answer,A|B,C\n"
        "essay,Unknown type,,x\n"
        "matching,Broken json,{oops,,\n"
    )
    response = client.post(f"/api/v1/admin/sections/{section_id}/questions/import-csv", json={"csv": bad}, headers=as_user(admin))
    assert response.status_code == 422
    problems = [d["msg"] for d in response.json()["error"]["details"]]
    assert len(problems) == 3
    assert any("Row 3" in p for p in problems) and any("Row 4" in p for p in problems) and any("Row 5" in p for p in problems)
    assert client.get(f"/api/v1/admin/sections/{section_id}/questions", headers=as_user(admin)).json() == []


def test_csv_import_needs_required_columns_and_a_draft(client, session):
    admin = _admin(session)
    _, section_id = _draft_reading_test(client, admin)
    response = client.post(f"/api/v1/admin/sections/{section_id}/questions/import-csv", json={"csv": "a,b\n1,2\n"}, headers=as_user(admin))
    assert response.status_code == 422
    published = publish_test(session, "reading", "academic")
    section = session.exec(select(Section).where(Section.test_version_id == published.current_version_id)).one()
    response = client.post(f"/api/v1/admin/sections/{section.id}/questions/import-csv", json={"csv": CSV}, headers=as_user(admin))
    assert response.status_code == 409


# --- analytics -------------------------------------------------------------


def _take(client, user, test_id, answers):
    attempt = client.post("/api/v1/attempts", json={"test_id": str(test_id)}, headers=as_user(user)).json()
    content = client.get(f"/api/v1/attempts/{attempt['id']}/content", headers=as_user(user)).json()
    for question, answer in zip(content["sections"][0]["questions"], answers):
        if answer is not None:
            client.put(f"/api/v1/attempts/{attempt['id']}/responses/{question['id']}", json={"response": answer}, headers=as_user(user))
    assert client.post(f"/api/v1/attempts/{attempt['id']}/submit", headers=as_user(user)).status_code == 200


def test_question_analytics(client, session):
    admin = _admin(session)
    test = publish_test(session, "reading", "academic", kind="practice")
    _take(client, make_user(session, "A"), test.id, ["London", "Tuesday"])
    _take(client, make_user(session, "B"), test.id, ["paris", None])
    _take(client, make_user(session, "C"), test.id, ["Paris", "tuesday"])

    data = client.get(f"/api/v1/admin/tests/{test.id}/analytics", headers=as_user(admin)).json()
    assert data["scored"] is True
    assert data["summary"]["attempts_finished"] == 3 and data["summary"]["completion_rate"] == 1.0
    q1, q2 = data["questions"]
    assert q1["correct"] == 1 and q1["correct_rate"] == round(1 / 3, 3)
    assert q1["common_wrong_answers"][0] == {"answer": "paris", "count": 2}
    assert q2["answered"] == 2 and q2["skipped"] == 1 and q2["correct"] == 2
    # No student names or ids in analytics.
    assert "student" not in str(data).lower()

    platform = client.get("/api/v1/admin/analytics", headers=as_user(admin)).json()
    assert platform["active_students"] == 3 and platform["by_module"]["reading"]["finished"] == 3
    assert platform["top_tests"][0]["attempts"] == 3

    student = make_user(session)
    assert client.get(f"/api/v1/admin/tests/{test.id}/analytics", headers=as_user(student)).status_code == 403
    assert client.get("/api/v1/admin/analytics", headers=as_user(student)).status_code == 403


def test_writing_analytics_are_unscored(client, session):
    admin = _admin(session)
    test = publish_test(session, "writing", "academic", kind="practice")
    data = client.get(f"/api/v1/admin/tests/{test.id}/analytics", headers=as_user(admin)).json()
    assert data["scored"] is False and data["questions"] == [] and data["summary"]["average_band"] is None


# --- limits, headers, health -----------------------------------------------


def test_autosave_is_rate_limited_per_account(client, session):
    from app.services.rate_limit import autosave_limiter

    user = make_user(session)
    test = publish_test(session, "reading", "academic", kind="practice")
    attempt = client.post("/api/v1/attempts", json={"test_id": str(test.id)}, headers=as_user(user)).json()
    content = client.get(f"/api/v1/attempts/{attempt['id']}/content", headers=as_user(user)).json()
    qid = content["sections"][0]["questions"][0]["id"]
    url = f"/api/v1/attempts/{attempt['id']}/responses/{qid}"
    original = autosave_limiter.max_calls
    autosave_limiter.max_calls = 3
    try:
        codes = [client.put(url, json={"response": f"v{i}"}, headers=as_user(user)).status_code for i in range(4)]
        assert codes == [200, 200, 200, 429]
        limited = client.put(url, json={"response": "again"}, headers=as_user(user)).json()
        assert limited["error"]["code"] == "rate_limited" and limited["error"]["retryable"] is True
        # Another account is unaffected.
        other = make_user(session)
        other_attempt = client.post("/api/v1/attempts", json={"test_id": str(test.id)}, headers=as_user(other)).json()
        assert client.put(f"/api/v1/attempts/{other_attempt['id']}/responses/{qid}", json={"response": "x"}, headers=as_user(other)).status_code == 200
    finally:
        autosave_limiter.max_calls = original


def test_oversized_body_is_refused(client, session):
    user = make_user(session)
    response = client.post(
        "/api/v1/attempts", content=b"x" * 2_000_001, headers={**as_user(user), "Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_security_headers_and_no_store(client, session):
    user = make_user(session)
    response = client.get("/api/v1/me/attempts", headers=as_user(user))
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "cache-control" not in client.get("/api/v1/health").headers


def test_readiness_check(client):
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200 and response.json() == {"status": "ok", "database": "ok"}


def test_health_checks_accept_head(client):
    # Uptime monitors probe with HEAD; a 405 shows up as "down".
    for path in ("/api/v1/health", "/api/v1/health/ready"):
        response = client.head(path)
        assert response.status_code == 200 and response.content == b""


def test_unexpected_errors_never_leak_details(client, session, monkeypatch):
    from app.api.v1 import admin_ops

    admin = _admin(session)

    def boom(*_args, **_kwargs):
        raise RuntimeError("secret connection string postgres://user:pw@host")

    monkeypatch.setattr(admin_ops, "_parse_csv_rows", boom)
    _, section_id = _draft_reading_test(client, admin)
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as quiet:
        response = quiet.post(f"/api/v1/admin/sections/{section_id}/questions/import-csv", json={"csv": CSV}, headers=as_user(admin))
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error" and "postgres" not in response.text
    assert body["error"]["correlation_id"]


# --- negative paths (scenarios 3, 11, 12, 17) ---------------------------------


def test_requests_without_a_token_are_rejected_everywhere(client):
    some_id = str(uuid4())
    for method, url in [
        ("get", "/api/v1/me"),
        ("get", "/api/v1/tests"),
        ("post", "/api/v1/attempts"),
        ("get", f"/api/v1/attempts/{some_id}"),
        ("get", f"/api/v1/attempts/{some_id}/results"),
        ("get", "/api/v1/certificates"),
        ("get", "/api/v1/admin/audit"),
    ]:
        assert getattr(client, method)(url).status_code == 401, url


def test_student_b_cannot_read_or_change_student_a_attempt(client, session):
    a, b = make_user(session, "A"), make_user(session, "B")
    test = publish_test(session, "reading", "academic", kind="practice")
    attempt = client.post("/api/v1/attempts", json={"test_id": str(test.id)}, headers=as_user(a)).json()
    content = client.get(f"/api/v1/attempts/{attempt['id']}/content", headers=as_user(a)).json()
    qid = content["sections"][0]["questions"][0]["id"]
    for method, url, kwargs in [
        ("get", f"/api/v1/attempts/{attempt['id']}", {}),
        ("get", f"/api/v1/attempts/{attempt['id']}/content", {}),
        ("put", f"/api/v1/attempts/{attempt['id']}/responses/{qid}", {"json": {"response": "hijack"}}),
        ("post", f"/api/v1/attempts/{attempt['id']}/submit", {}),
        ("get", f"/api/v1/attempts/{attempt['id']}/results", {}),
    ]:
        assert getattr(client, method)(url, headers=as_user(b), **kwargs).status_code == 404, url


def test_answer_keys_and_drafts_never_reach_students(client, session):
    admin, student = _admin(session), make_user(session)
    published = publish_test(session, "reading", "academic", kind="practice")
    draft_id, section_id = _draft_reading_test(client, admin)
    client.post(f"/api/v1/admin/sections/{section_id}/questions/import-csv", json={"csv": CSV}, headers=as_user(admin))

    catalog = client.get("/api/v1/tests", headers=as_user(student)).json()
    assert draft_id not in {t["id"] for t in catalog}
    assert client.get(f"/api/v1/tests/{draft_id}", headers=as_user(student)).status_code == 404
    assert client.post("/api/v1/attempts", json={"test_id": draft_id}, headers=as_user(student)).status_code in (404, 409)

    detail = client.get(f"/api/v1/tests/{published.id}", headers=as_user(student))
    assert detail.status_code == 200
    text = detail.text
    for leaked in ("correct_answers", "accepted_aliases", "rationale", "London", "Tuesday"):
        assert leaked not in text, leaked
    assert client.get(f"/api/v1/admin/tests/{published.id}/content", headers=as_user(student)).status_code == 403


def test_duplicate_submit_scores_once(client, session):
    user = make_user(session)
    test = publish_test(session, "reading", "academic", kind="practice")
    attempt = client.post("/api/v1/attempts", json={"test_id": str(test.id)}, headers=as_user(user)).json()
    content = client.get(f"/api/v1/attempts/{attempt['id']}/content", headers=as_user(user)).json()
    qid = content["sections"][0]["questions"][0]["id"]
    client.put(f"/api/v1/attempts/{attempt['id']}/responses/{qid}", json={"response": "London"}, headers=as_user(user))
    first = client.post(f"/api/v1/attempts/{attempt['id']}/submit", headers=as_user(user)).json()
    second = client.post(f"/api/v1/attempts/{attempt['id']}/submit", headers=as_user(user)).json()
    assert first["raw_score"] == second["raw_score"] == 1
    assert first["submitted_at"] == second["submitted_at"]
    # A save after submission is refused rather than silently changing the score.
    assert client.put(f"/api/v1/attempts/{attempt['id']}/responses/{qid}", json={"response": "x"}, headers=as_user(user)).status_code == 409


def test_new_version_does_not_change_an_open_attempt(client, session):
    """Scenario 11: an attempt keeps the version it started on."""

    admin, user = _admin(session), make_user(session)
    test = publish_test(session, "reading", "academic", kind="practice")
    attempt = client.post("/api/v1/attempts", json={"test_id": str(test.id)}, headers=as_user(user)).json()
    before = client.get(f"/api/v1/attempts/{attempt['id']}/content", headers=as_user(user)).json()

    # Admin edits by duplicate -> publish copy -> archive original.
    copy = client.post(f"/api/v1/admin/tests/{test.id}/duplicate", headers=as_user(admin)).json()
    session.expire_all()
    copy_version = session.get(models.TestVersion, session.get(models.Test, UUID(copy["id"])).current_version_id)
    assert copy_version.id != test.current_version_id
    client.post(f"/api/v1/admin/tests/{test.id}/archive", headers=as_user(admin))

    after = client.get(f"/api/v1/attempts/{attempt['id']}/content", headers=as_user(user)).json()
    assert [q["id"] for q in after["sections"][0]["questions"]] == [q["id"] for q in before["sections"][0]["questions"]]
    qid = before["sections"][0]["questions"][0]["id"]
    client.put(f"/api/v1/attempts/{attempt['id']}/responses/{qid}", json={"response": "London"}, headers=as_user(user))
    assert client.post(f"/api/v1/attempts/{attempt['id']}/submit", headers=as_user(user)).json()["raw_score"] == 1


def test_role_cannot_be_self_assigned_through_profile_update(client, session):
    user = make_user(session)
    response = client.patch("/api/v1/me", json={"role": "admin", "display_name": "Still student"}, headers=as_user(user))
    assert response.status_code in (200, 422)
    assert client.get("/api/v1/admin/ping", headers=as_user(user)).status_code == 403


def test_audit_timestamps_are_recent(client, session):
    admin = _admin(session)
    _draft_reading_test(client, admin)
    entry = session.exec(select(AdminAuditLog)).first()
    created = entry.created_at if entry.created_at.tzinfo else entry.created_at.replace(tzinfo=timezone.utc)
    assert (datetime.now(timezone.utc) - created).total_seconds() < 60

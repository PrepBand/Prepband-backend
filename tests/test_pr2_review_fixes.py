"""Fixes from the PR #2 review: grading by question type, answer-shape
validation, publish rules, CSV round trips, shared resource uploads, the
streaming body limit, audit coverage and the backup exit code."""

from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.api.v1.admin_ops import _cell_in, _cell_out
from app.models import AdminAuditLog, Section
from app.models import Test as TestRow
from app.models import TestVersion as VersionRow
from app.services import audit
from app.services.content_io import question_problem
from app.services.grading import credit, grade_responses, is_correct

from .conftest import as_user, make_user, publish_test
from .test_resources import PDF, PNG, _admin, _category, _upload, bucket  # noqa: F401 - bucket is a fixture

# --- grading follows the question type, not the response's shape -----------


def test_a_single_string_cant_earn_full_marks_on_multiple_select():
    key = ["A", "C"]
    assert credit("A", key, None, None, "multiple_select") == 0.0
    assert credit(["A"], key, None, None, "multiple_select") == 0.5
    assert credit(["A", "C"], key, None, None, "multiple_select") == 1.0


def test_a_list_cant_skip_a_text_questions_word_limit():
    key = ["salt deposits", "salt"]
    options = {"max_words": 1}
    assert credit(["salt deposits", "salt"], key, None, options, "short_answer") == 0.0
    assert credit("salt", key, None, options, "short_answer") == 1.0
    assert credit("salt deposits", key, None, options, "short_answer") == 0.0


def test_each_type_still_grades_its_own_shape():
    assert is_correct("B", ["B"], None, None, "multiple_choice")
    assert not is_correct(["B"], ["B"], None, None, "multiple_choice")
    assert is_correct("True", ["True"], None, None, "true_false_not_given")
    assert credit({"0": "X", "1": "Y"}, {"0": "X", "1": "Y"}, None, None, "matching") == 1.0
    assert credit("X", {"0": "X"}, None, None, "matching") == 0.0


def test_grade_responses_uses_the_question_type():
    question = SimpleNamespace(id=1, type="multiple_select", marks=2, options=["A", "B", "C"])
    key = SimpleNamespace(correct_answers=["A", "C"], accepted_aliases=None)
    assert grade_responses([(question, key)], {1: SimpleNamespace(response="A")}) == (0.0, 2)
    assert grade_responses([(question, key)], {1: SimpleNamespace(response=["A"])}) == (1.0, 2)


# --- answer validation never crashes ---------------------------------------


@pytest.mark.parametrize("answer", [["True", "False"], {"a": 1}, [["True"]], 1])
def test_bad_true_false_answers_are_a_problem_not_a_crash(answer):
    assert "answer must be one of" in question_problem("true_false_not_given", None, answer)
    assert "answer must be one of" in question_problem("yes_no_not_given", None, answer)


# --- publish: every section matches its test ----------------------------------


def test_a_reading_test_with_only_a_writing_section_cant_publish(client, session):
    admin = make_user(session, "Ada Admin", role="admin")
    created = client.post(
        "/api/v1/admin/tests",
        json={"title": "Odd reading", "module": "reading", "track": "academic", "kind": "practice"},
        headers=as_user(admin),
    )
    assert created.status_code in (200, 201), created.text
    test_id = created.json()["id"]
    section = client.post(f"/api/v1/admin/tests/{test_id}/sections", json={"module": "writing", "order_index": 1}, headers=as_user(admin))
    assert section.status_code == 201, section.text
    prompt = client.post(
        f"/api/v1/admin/sections/{section.json()['id']}/writing-prompts",
        json={"task_number": 1, "prompt": "Describe the chart.", "display_order": 1},
        headers=as_user(admin),
    )
    assert prompt.status_code == 201, prompt.text

    published = client.post(f"/api/v1/admin/tests/{test_id}/publish", headers=as_user(admin))
    assert published.status_code == 422
    assert any("must be a Reading section" in d["msg"] for d in published.json()["error"]["details"])


# --- CSV export/import round trip ------------------------------------------


@pytest.mark.parametrize("value", ["True", "Not Given", ["A", "C"], ["London"], {"0": "A"}, ["a|b"], None])
def test_csv_cells_round_trip(value):
    back = _cell_in(_cell_out(value))
    if isinstance(value, str):
        assert back in (value, [value])
    else:
        assert back == value


def test_a_json_quoted_scalar_imports_without_quotes():
    assert _cell_in('"True"') == "True"


def test_exported_scalar_true_false_key_re_imports(client, session):
    admin = make_user(session, "Ada Admin", role="admin")
    test = publish_test(session, "reading", "academic", kind="practice")
    version = session.get(VersionRow, test.current_version_id)
    version.status = "draft"
    test.status = "draft"
    session.add_all([version, test])
    session.commit()
    section = session.exec(select(Section).where(Section.test_version_id == version.id)).one()

    csv_text = "display_order,type,prompt,marks,options,correct_answers,accepted_aliases,rationale\n" "1,true_false_not_given,Is it?,1,,True,,\n"
    imported = client.post(
        f"/api/v1/admin/sections/{section.id}/questions/import-csv", json={"csv": csv_text, "mode": "replace"}, headers=as_user(admin)
    )
    assert imported.status_code == 200, imported.text
    exported = client.get(f"/api/v1/admin/sections/{section.id}/questions.csv", headers=as_user(admin))
    assert exported.status_code == 200
    assert ',"""True"""' not in exported.text and ",True," in exported.text
    again = client.post(
        f"/api/v1/admin/sections/{section.id}/questions/import-csv", json={"csv": exported.text, "mode": "replace"}, headers=as_user(admin)
    )
    assert again.status_code == 200, again.text


# --- resource uploads have one owner ------------------------------------------


def test_two_resources_cant_share_an_upload(client, session, bucket):  # noqa: F811
    admin = _admin(session)
    category = _category(client, admin)
    cover = _upload(client, admin, bucket, "cover", "image/png", PNG)
    base = {"category_id": category["id"], "kind": "pdf", "cover_key": cover, "cover_alt": "Books"}
    first = client.post("/api/v1/admin/resources", json={**base, "title": "First"}, headers=as_user(admin))
    assert first.status_code == 201, first.text
    second = client.post("/api/v1/admin/resources", json={**base, "title": "Second"}, headers=as_user(admin))
    assert second.status_code == 409

    other = client.post(
        "/api/v1/admin/resources", json={**base, "title": "Third", "cover_key": None}, headers=as_user(admin)
    )
    assert other.status_code == 201, other.text
    patched = client.patch(f"/api/v1/admin/resources/{other.json()['id']}", json={"cover_key": cover}, headers=as_user(admin))
    assert patched.status_code == 409
    # Deleting the second resource leaves the first one's cover alone.
    assert client.delete(f"/api/v1/admin/resources/{other.json()['id']}", headers=as_user(admin)).status_code == 204
    assert cover not in bucket.deleted
    assert PDF  # imported for parity with the resource tests


# --- request body limit counts real bytes ----------------------------------------


def test_a_chunked_body_over_the_limit_is_refused(client):
    def chunks():
        for _ in range(25):
            yield b" " * 100_000

    response = client.post("/api/v1/client-events", content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


# --- audit coverage ------------------------------------------------------------


def test_bulk_sensitive_reads_are_audited():
    assert "/admin/sections/{section_id}/questions.csv" in audit.SENSITIVE_READS
    assert "/admin/certificates" in audit.SENSITIVE_READS


def test_an_unexpected_admin_failure_is_still_audited(client, session, monkeypatch):
    admin = make_user(session, "Ada Admin", role="admin")
    test = publish_test(session, "reading", "academic", kind="practice")
    draft = session.get(TestRow, test.id)
    draft.status = "draft"
    version = session.get(VersionRow, test.current_version_id)
    version.status = "draft"
    session.add_all([draft, version])
    session.commit()

    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("app.api.v1.admin.validate_for_publish", boom)
    with pytest.raises(RuntimeError):
        client.post(f"/api/v1/admin/tests/{test.id}/publish", headers=as_user(admin))
    session.expire_all()
    entries = session.exec(select(AdminAuditLog).where(AdminAuditLog.action == "POST /admin/tests/{test_id}/publish")).all()
    assert [(e.outcome, e.status_code) for e in entries] == [("failed", 500)]


# --- backup exit code ----------------------------------------------------------


def test_backup_dumps_accounts_and_data_in_one_run(monkeypatch, tmp_path):
    """PR #2 review: two pg_dump runs could see different moments, leaving a
    profile without its account. One run reads one snapshot."""

    from scripts import backup

    calls = []
    monkeypatch.setattr(backup.shutil, "which", lambda name: "pg_dump")

    def fake_run(args, **_kwargs):
        calls.append(args)
        (tmp_path / "database.sql").write_text("-- data", encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    assert backup.dump_database("postgresql://x/y", tmp_path) is True
    assert len(calls) == 1
    assert {"--data-only", "--table=auth.users", "--table=auth.identities", "--table=public.*"} <= set(calls[0])


def test_backup_fails_when_the_database_dump_fails(monkeypatch, tmp_path):
    from scripts import backup

    monkeypatch.setattr(backup, "ROOT", tmp_path)
    monkeypatch.setattr(backup, "get_settings", lambda: SimpleNamespace(database_url="postgresql://x/y"))
    monkeypatch.setattr(backup, "dump_database", lambda url, out: False)
    monkeypatch.setattr(backup, "export_content", lambda url, out: 0)
    monkeypatch.setattr("sys.argv", ["backup"])
    assert backup.main() == 1
    monkeypatch.setattr(backup, "ROOT", tmp_path / "content-only")
    monkeypatch.setattr("sys.argv", ["backup", "--content-only"])
    assert backup.main() == 0

"""The database's own rules, on real Postgres: frozen attempts, frozen
published content, append-only audit, row-level security, grants, account
deletion, and the row locks that stop simultaneous requests from racing.
(ADR-003: the database owns these; SQLite tests can't run them.)"""

import threading
import uuid

import psycopg
import pytest

from ..conftest import as_user, publish_test
from .conftest import as_role, create_user, reset_role

pytestmark = pytest.mark.postgres


def _start_and_answer(client, session, user, answer="London"):
    test = publish_test(session, "reading", "academic", kind="practice")
    attempt = client.post("/api/v1/attempts", json={"test_id": str(test.id)}, headers=as_user(user))
    assert attempt.status_code == 200, attempt.text
    attempt_id = attempt.json()["id"]
    content = client.get(f"/api/v1/attempts/{attempt_id}/content", headers=as_user(user)).json()
    question_id = content["sections"][0]["questions"][0]["id"]
    saved = client.put(f"/api/v1/attempts/{attempt_id}/responses/{question_id}", json={"response": answer}, headers=as_user(user))
    assert saved.status_code == 200, saved.text
    return test, attempt_id, question_id


def test_every_migration_applies_and_the_models_match_the_schema(pg_client, pg_session, pg):
    """Getting this far means 0000-latest applied cleanly. A full student
    flow through the API then proves the SQLModel models line up with the
    real columns (a model/schema drift would fail here, not in production)."""

    user = create_user(pg)
    _, attempt_id, _ = _start_and_answer(pg_client, pg_session, user)
    submitted = pg_client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=as_user(user))
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["raw_score"] == 1
    results = pg_client.get(f"/api/v1/attempts/{attempt_id}/results", headers=as_user(user))
    assert results.status_code == 200, results.text
    history = pg_client.get("/api/v1/me/attempts", headers=as_user(user))
    assert history.status_code == 200 and history.json()["total"] == 1


def test_signup_creates_a_profile_and_a_student_role(pg):
    user = create_user(pg, "Grace Hopper")
    profile = pg.execute("select display_name, avatar_initials from public.profiles where id = %s", (user,)).fetchone()
    assert profile == ("Grace Hopper", "GH")
    assert pg.execute("select role from public.user_roles where user_id = %s", (user,)).fetchone() == ("student",)


def test_a_finished_attempt_cannot_be_changed_even_directly(pg_client, pg_session, pg):
    user = create_user(pg)
    _, attempt_id, _ = _start_and_answer(pg_client, pg_session, user)
    pg_client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=as_user(user))

    with pytest.raises(psycopg.errors.RaiseException, match="no longer editable"):
        pg.execute("update public.attempts set raw_score = 99 where id = %s", (attempt_id,))
    with pytest.raises(psycopg.errors.RaiseException, match="no longer editable"):
        pg.execute("delete from public.attempts where id = %s", (attempt_id,))
    # Frozen marks are frozen with it.
    with pytest.raises(psycopg.errors.RaiseException):
        pg.execute("update public.attempts set question_marks = '{}'::jsonb where id = %s", (attempt_id,))


def test_finished_writing_answers_are_frozen(pg_client, pg_session, pg):
    user = create_user(pg)
    test = publish_test(pg_session, "writing", "academic", kind="practice")
    attempt_id = pg_client.post("/api/v1/attempts", json={"test_id": str(test.id)}, headers=as_user(user)).json()["id"]
    content = pg_client.get(f"/api/v1/attempts/{attempt_id}/content", headers=as_user(user)).json()
    prompt_id = content["sections"][0]["writing_prompts"][0]["id"]
    pg_client.put(f"/api/v1/attempts/{attempt_id}/writing/{prompt_id}", json={"body": "My essay"}, headers=as_user(user))
    assert pg_client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=as_user(user)).status_code == 200

    with pytest.raises(psycopg.errors.RaiseException, match="can no longer change"):
        pg.execute("update public.writing_responses set body = 'changed' where attempt_id = %s", (attempt_id,))


def test_deleting_an_account_removes_its_finished_attempts(pg_client, pg_session, pg):
    """Migration 0010: the privacy policy promises deletion. Never verified
    live (the check was rolled back), so it is verified here."""

    user = create_user(pg)
    _, attempt_id, _ = _start_and_answer(pg_client, pg_session, user)
    pg_client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=as_user(user))

    pg.execute("delete from auth.users where id = %s", (user,))
    assert pg.execute("select count(*) from public.attempts where id = %s", (attempt_id,)).fetchone() == (0,)
    assert pg.execute("select count(*) from public.responses where attempt_id = %s", (attempt_id,)).fetchone() == (0,)
    assert pg.execute("select count(*) from public.profiles where id = %s", (user,)).fetchone() == (0,)


def test_a_published_version_is_immutable(pg_session, pg):
    test = publish_test(pg_session, "reading", "academic")
    with pytest.raises(psycopg.errors.RaiseException, match="published and immutable"):
        pg.execute("update public.test_versions set version_number = 9 where id = %s", (str(test.current_version_id),))


def test_the_audit_log_is_append_only(pg):
    pg.execute(
        "insert into public.admin_audit_log (actor_id, actor_role, action, method, path, outcome, status_code)"
        " values (%s, 'admin', 'test.create', 'POST', '/api/v1/admin/tests', 'success', 201)",
        (str(uuid.uuid4()),),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        pg.execute("update public.admin_audit_log set outcome = 'denied'")


# --- row-level security and grants (what a browser could do with the anon key) ---


def test_students_see_only_their_own_rows(pg_client, pg_session, pg):
    alice, bob = create_user(pg, "Alice Ng"), create_user(pg, "Bob Lee")
    _, alice_attempt, _ = _start_and_answer(pg_client, pg_session, alice)

    as_role(pg, "authenticated", bob)
    try:
        assert pg.execute("select count(*) from public.attempts where id = %s", (alice_attempt,)).fetchone() == (0,)
        assert pg.execute("select count(*) from public.responses").fetchone() == (0,)
        assert pg.execute("select count(*) from public.profiles").fetchone() == (1,)  # only Bob's own
    finally:
        reset_role(pg)

    as_role(pg, "authenticated", alice)
    try:
        assert pg.execute("select count(*) from public.attempts where id = %s", (alice_attempt,)).fetchone() == (1,)
    finally:
        reset_role(pg)


def test_answer_keys_and_drafts_are_hidden_from_browsers(pg_session, pg):
    publish_test(pg_session, "reading", "academic")
    user = create_user(pg)
    for role, who in (("anon", None), ("authenticated", user)):
        as_role(pg, role, who)
        try:
            assert pg.execute("select count(*) from public.answer_keys").fetchone() == (0,)
        finally:
            reset_role(pg)


def test_nobody_can_make_themselves_admin(pg):
    user = create_user(pg)
    as_role(pg, "authenticated", user)
    try:
        pg.execute("update public.user_roles set role = 'admin' where user_id = %s", (user,))
    finally:
        reset_role(pg)
    # RLS has no update policy: the statement matches no rows.
    assert pg.execute("select role from public.user_roles where user_id = %s", (user,)).fetchone() == ("student",)


def test_a_student_cannot_forge_their_email(pg):
    """Migration 0013: a column-level revoke alone didn't work (the table-
    level grant overrode it), so a student could rewrite their email."""

    user = create_user(pg)
    as_role(pg, "authenticated", user)
    try:
        for column in ("email", "email_confirmed_at", "avatar_initials"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                pg.execute(f"update public.profiles set {column} = null where id = %s", (user,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pg.execute("insert into public.profiles (id, email) values (%s, 'x@example.test')", (str(uuid.uuid4()),))
        # What the app does write still works.
        pg.execute(
            "update public.profiles set onboarding_completed = true, avatar_url = 'https://r2.example/a.png',"
            " display_name = 'New Name' where id = %s",
            (user,),
        )
    finally:
        reset_role(pg)
    assert pg.execute("select onboarding_completed, display_name from public.profiles where id = %s", (user,)).fetchone() == (
        True,
        "New Name",
    )


def test_email_lookup_is_closed_to_browsers(pg):
    """Migration 0011: no account enumeration through the API."""

    for role in ("anon", "authenticated"):
        as_role(pg, role)
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                pg.execute("select public.is_email_registered('someone@example.test')")
        finally:
            reset_role(pg)


# --- row locks: simultaneous requests ------------------------------------------


def _in_parallel(count, call):
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def run(index):
        barrier.wait()
        results[index] = call()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def test_simultaneous_submits_grade_once_without_errors(pg_client, pg_session, pg):
    user = create_user(pg)
    _, attempt_id, _ = _start_and_answer(pg_client, pg_session, user)

    responses = _in_parallel(6, lambda: pg_client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=as_user(user)))
    assert [r.status_code for r in responses] == [200] * 6
    assert {r.json()["submitted_at"] for r in responses} == {responses[0].json()["submitted_at"]}
    assert {r.json()["raw_score"] for r in responses} == {1}


def test_simultaneous_first_saves_of_one_answer_never_fail(pg_client, pg_session, pg):
    user = create_user(pg)
    test = publish_test(pg_session, "reading", "academic", kind="practice")
    attempt_id = pg_client.post("/api/v1/attempts", json={"test_id": str(test.id)}, headers=as_user(user)).json()["id"]
    content = pg_client.get(f"/api/v1/attempts/{attempt_id}/content", headers=as_user(user)).json()
    question_id = content["sections"][0]["questions"][0]["id"]
    url = f"/api/v1/attempts/{attempt_id}/responses/{question_id}"

    responses = _in_parallel(
        6, lambda: pg_client.put(url, json={"response": "London", "client_seq": 1000}, headers=as_user(user))
    )
    assert [r.status_code for r in responses] == [200] * 6
    assert pg.execute("select count(*) from public.responses where attempt_id = %s", (attempt_id,)).fetchone() == (1,)


def test_a_save_racing_the_submit_is_refused_cleanly(pg_client, pg_session, pg):
    user = create_user(pg)
    _, attempt_id, question_id = _start_and_answer(pg_client, pg_session, user)
    url = f"/api/v1/attempts/{attempt_id}/responses/{question_id}"

    def call(index):
        if index % 2:
            return pg_client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=as_user(user))
        return pg_client.put(url, json={"response": "Paris", "client_seq": 5000 + index}, headers=as_user(user))

    counter = iter(range(100))
    responses = _in_parallel(6, lambda: call(next(counter)))
    # Saves either land before the submit (200) or are refused after it
    # (409); never a 500.
    assert all(r.status_code in (200, 409) for r in responses), [r.status_code for r in responses]
    final = pg.execute("select status, raw_score from public.attempts where id = %s", (attempt_id,)).fetchone()
    assert final[0] == "submitted"


def test_review_my_mistakes_runs_on_postgres(pg_client, pg_session, pg):
    user = create_user(pg)
    _, attempt_id, _ = _start_and_answer(pg_client, pg_session, user, answer="Paris")
    pg_client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=as_user(user))
    page = pg_client.get("/api/v1/me/mistakes", headers=as_user(user))
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 2  # one wrong, one skipped

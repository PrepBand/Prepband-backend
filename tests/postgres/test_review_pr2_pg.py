"""PR #2 review fixes that only real Postgres can check: migration 0015's
grants (no direct writes to server-owned tables, results-only columns
hidden) and the row locks behind simultaneous starts, renumbering,
completing and abandoning mocks."""

import psycopg
import pytest

from ..conftest import as_user, publish_full_mock, publish_test
from ..test_full_mock import _finish_module, _start_module
from .conftest import as_role, create_user, reset_role
from .test_database_rules import _in_parallel, _start_and_answer

pytestmark = pytest.mark.postgres


def test_students_cant_write_server_owned_tables_directly(pg_client, pg_session, pg):
    """A browser could rewrite its own open attempt (scores, status,
    deadline) or its answers through the Data API."""

    user = create_user(pg)
    _, attempt_id, question_id = _start_and_answer(pg_client, pg_session, user)
    as_role(pg, "authenticated", user)
    try:
        # Reading your own rows still works.
        assert pg.execute("select count(*) from public.attempts where id = %s", (attempt_id,)).fetchone() == (1,)
        assert pg.execute("select count(*) from public.responses where attempt_id = %s", (attempt_id,)).fetchone() == (1,)
        statements = [
            ("update public.attempts set raw_score = 99, max_score = 99, status = 'submitted' where id = %s", (attempt_id,)),
            ("update public.attempts set expires_at = now() + interval '1 year' where id = %s", (attempt_id,)),
            ("delete from public.attempts where id = %s", (attempt_id,)),
            ("update public.responses set client_seq = 1 where attempt_id = %s", (attempt_id,)),
            ("delete from public.responses where attempt_id = %s", (attempt_id,)),
            ("delete from public.writing_responses where attempt_id = %s", (attempt_id,)),
            ("delete from public.speaking_responses where attempt_id = %s", (attempt_id,)),
            ("delete from public.self_reviews where attempt_id = %s", (attempt_id,)),
            ("delete from public.mock_attempts where student_id = %s", (user,)),
            ("delete from public.certificates where student_id = %s", (user,)),
        ]
        for sql, params in statements:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                pg.execute(sql, params)
    finally:
        reset_role(pg)
    row = pg.execute("select status, raw_score from public.attempts where id = %s", (attempt_id,)).fetchone()
    assert row == ("in_progress", None)
    assert question_id


def test_results_only_columns_are_hidden_from_browsers(pg_session, pg):
    listening = publish_test(pg_session, "listening", "academic")
    publish_test(pg_session, "writing", "academic")
    pg.execute(
        "insert into public.media_assets (section_id, type, storage_key, transcript)"
        " select id, 'audio', 'listening/a.mp3', 'the answers' from public.sections where test_version_id = %s",
        (str(listening.current_version_id),),
    )
    pg.execute("update public.writing_prompts set examiner_notes = 'model answer'")
    user = create_user(pg)
    for role, who in (("anon", None), ("authenticated", user)):
        as_role(pg, role, who)
        try:
            # What the test page needs is still readable ...
            assert pg.execute("select count(storage_key) from public.media_assets").fetchone() == (1,)
            assert pg.execute("select count(prompt) from public.writing_prompts").fetchone()[0] >= 1
            # ... but not what's reserved for results.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                pg.execute("select transcript from public.media_assets")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                pg.execute("select examiner_notes from public.writing_prompts")
        finally:
            reset_role(pg)


def test_simultaneous_starts_create_one_attempt(pg_client, pg_session, pg):
    user = create_user(pg)
    test = publish_test(pg_session, "reading", "academic", kind="practice")
    responses = _in_parallel(6, lambda: pg_client.post("/api/v1/attempts", json={"test_id": str(test.id)}, headers=as_user(user)))
    assert [r.status_code for r in responses] == [200] * 6
    assert len({r.json()["id"] for r in responses}) == 1
    assert pg.execute("select count(*) from public.attempts where student_id = %s", (user,)).fetchone() == (1,)


def test_renumber_from_zero_based_orders(pg_client, pg_session, pg):
    admin = create_user(pg, "Ada Admin", role="admin")
    test = publish_test(pg_session, "reading", "academic", kind="practice")
    version_id = str(test.current_version_id)
    # A draft copy of the same content (published versions are frozen).
    pg.execute("update public.tests set status = 'draft' where id = %s", (str(test.id),))
    pg.execute("alter table public.test_versions disable trigger user")
    try:
        pg.execute("update public.test_versions set status = 'draft', published_at = null where id = %s", (version_id,))
    finally:
        pg.execute("alter table public.test_versions enable trigger user")
    section_id = pg.execute("select id from public.sections where test_version_id = %s", (version_id,)).fetchone()[0]
    # JSON imports number questions from 0.
    pg.execute("update public.questions set display_order = -display_order where section_id = %s", (section_id,))
    pg.execute("update public.questions set display_order = -display_order - 1 where section_id = %s", (section_id,))
    pg.execute(
        "insert into public.questions (section_id, type, prompt, display_order, marks) values (%s, 'short_answer', 'Q3', 2, 1)",
        (section_id,),
    )
    response = pg_client.patch(f"/api/v1/admin/sections/{section_id}/questions/bulk", json={"action": "renumber"}, headers=as_user(admin))
    assert response.status_code == 200, response.text
    orders = pg.execute("select display_order from public.questions where section_id = %s order by display_order", (section_id,)).fetchall()
    assert [o[0] for o in orders] == [1, 2, 3]


def test_simultaneous_reads_complete_a_mock_once(pg_client, pg_session, pg):
    user = create_user(pg)
    publish_full_mock(pg_session)
    mock = pg_client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    for module in ("listening", "reading", "writing", "speaking"):
        _finish_module(pg_client, user, mock["id"], module)

    responses = _in_parallel(6, lambda: pg_client.get(f"/api/v1/mocks/{mock['id']}", headers=as_user(user)))
    assert [r.status_code for r in responses] == [200] * 6
    assert {r.json()["status"] for r in responses} == {"completed"}
    assert pg.execute("select count(*) from public.certificates").fetchone() == (1,)


def test_abandon_racing_a_submit_never_fails(pg_client, pg_session, pg):
    user = create_user(pg)
    publish_full_mock(pg_session)
    mock = pg_client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    attempt_id = _start_module(pg_client, user, mock["id"], "listening")["attempt_id"]

    def call(index):
        if index % 2:
            return pg_client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=as_user(user))
        return pg_client.post(f"/api/v1/mocks/{mock['id']}/abandon", headers=as_user(user))

    counter = iter(range(100))
    responses = _in_parallel(4, lambda: call(next(counter)))
    assert all(r.status_code in (200, 409) for r in responses), [r.status_code for r in responses]
    assert pg.execute("select status from public.mock_attempts where id = %s", (mock["id"],)).fetchone() == ("abandoned",)
    status = pg.execute("select status from public.attempts where id = %s", (attempt_id,)).fetchone()[0]
    assert status in ("submitted", "cancelled")


def test_advancing_after_an_expiry_creates_one_next_module(pg_client, pg_session, pg):
    user = create_user(pg)
    publish_full_mock(pg_session)
    mock = pg_client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    listening = _start_module(pg_client, user, mock["id"], "listening")["attempt_id"]
    pg.execute("update public.attempts set expires_at = now() - interval '1 minute' where id = %s", (listening,))

    url = f"/api/v1/mocks/{mock['id']}/modules/reading/start"
    responses = _in_parallel(6, lambda: pg_client.post(url, headers=as_user(user)))
    assert [r.status_code for r in responses] == [200] * 6, [r.text for r in responses]
    assert len({r.json()["attempt_id"] for r in responses}) == 1
    assert pg.execute("select count(*) from public.attempts where student_id = %s", (user,)).fetchone() == (2,)

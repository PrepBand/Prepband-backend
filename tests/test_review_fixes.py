"""Code review fixes (2026-11-25): save ordering, expiry in lists, IELTS
word limits and partial credit, spelling variants, and database conflicts
returning 409 instead of 500."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app import models
from app.core.errors import register_exception_handlers
from app.services.content_io import question_problem, with_word_limit, word_limit_from_instructions
from app.services.grading import credit, grade_responses, is_correct, word_count

from .conftest import as_user, make_user, publish_test

# --- grading -------------------------------------------------------------


def test_text_answer_over_the_word_limit_is_wrong():
    options = {"max_words": 2}
    assert is_correct("salt deposits", ["salt deposits"], None, options)
    assert not is_correct("the salt deposits", ["the salt deposits"], None, options)
    # Without a limit the same answer still matches.
    assert is_correct("the salt deposits", ["the salt deposits"], None, None)


def test_numbers_and_hyphenated_words_count_like_ielts():
    assert word_count("pond-dipping") == 1
    assert word_count("15 May") == 1
    assert word_count("£20 per night") == 2
    assert is_correct("15 May", ["15 May"], None, {"max_words": 1})


def test_spelling_variants_match_without_an_alias():
    assert is_correct("Colour", ["color"], None)
    assert is_correct("organize", ["organise"], None)
    assert is_correct("city center", ["city centre"], None)
    assert is_correct("pond dipping", ["pond-dipping"], None)
    assert is_correct("19th", ["19"], None)
    assert is_correct("1,000", ["1000"], None)
    assert not is_correct("London", ["Paris"], None)


def test_matching_with_several_prompts_earns_marks_per_item():
    key = {"0": "A", "1": "B", "2": "C", "3": "D"}
    assert credit({"0": "A", "1": "B", "2": "X", "3": "Y"}, key, None) == 0.5
    assert credit(key, key, None) == 1.0
    # A one-item matching question stays all-or-nothing.
    assert credit({"0": "A"}, {"0": "A"}, None) == 1.0


def test_multiple_select_extra_picks_cancel_correct_ones():
    key = ["A", "C"]
    assert credit(["A", "C"], key, None) == 1.0
    assert credit(["A"], key, None) == 0.5
    assert credit(["A", "B"], key, None) == 0.0
    assert credit(["A", "B", "C", "D", "E"], key, None) == 0.0


def test_grade_responses_adds_partial_marks():
    question = SimpleNamespace(id=1, marks=2, options={"prompts": ["a", "b"], "choices": ["X", "Y"]})
    key = SimpleNamespace(correct_answers={"0": "X", "1": "Y"}, accepted_aliases=None)
    response = SimpleNamespace(response={"0": "X", "1": "X"})
    assert grade_responses([(question, key)], {1: response}) == (1.0, 2)


# --- word limits in content ----------------------------------------------


def test_word_limit_is_read_from_section_instructions():
    assert word_limit_from_instructions("Complete the notes with NO MORE THAN TWO WORDS from the text.") == 2
    assert word_limit_from_instructions("Write ONE WORD ONLY for each answer.") == 1
    assert word_limit_from_instructions("Choose TRUE, FALSE or NOT GIVEN.") is None
    # Two different limits in one section: too ambiguous to apply.
    assert word_limit_from_instructions("Questions 1-5: ONE WORD ONLY. Questions 6-9: NO MORE THAN THREE WORDS.") is None


def test_word_limit_is_added_only_to_text_questions_without_one():
    text = "Write NO MORE THAN TWO WORDS."
    assert with_word_limit("gap_fill", None, text) == {"max_words": 2}
    assert with_word_limit("gap_fill", {"max_words": 3}, text) == {"max_words": 3}
    assert with_word_limit("multiple_choice", ["a", "b"], text) == ["a", "b"]


def test_max_words_is_validated():
    assert question_problem("short_answer", {"max_words": 2}, ["x"]) is None
    assert question_problem("short_answer", {"max_words": 0}, ["x"])
    assert question_problem("short_answer", {"max_words": "two"}, ["x"])
    assert question_problem("short_answer", ["a"], ["x"])


# --- save ordering ---------------------------------------------------------


def _start(client, session, module="reading"):
    user = make_user(session)
    test = publish_test(session, module, "academic", kind="practice")
    attempt = client.post("/api/v1/attempts", json={"test_id": str(test.id)}, headers=as_user(user)).json()
    content = client.get(f"/api/v1/attempts/{attempt['id']}/content", headers=as_user(user)).json()
    return user, attempt["id"], content


def test_an_older_save_arriving_late_does_not_overwrite_a_newer_one(client, session):
    user, attempt_id, content = _start(client, session)
    qid = content["sections"][0]["questions"][0]["id"]
    url = f"/api/v1/attempts/{attempt_id}/responses/{qid}"

    newer = client.put(url, json={"response": "London", "client_seq": 200}, headers=as_user(user))
    assert newer.status_code == 200 and newer.json()["client_seq"] == 200
    # The retried old save lands afterwards: ignored, the newer answer stays.
    late = client.put(url, json={"response": "Lon", "client_seq": 100}, headers=as_user(user))
    assert late.status_code == 200
    assert late.json()["response"] == "London"

    state = client.get(f"/api/v1/attempts/{attempt_id}", headers=as_user(user)).json()
    assert state["responses"][0]["response"] == "London"
    assert state["responses"][0]["client_seq"] == 200

    # A genuinely newer save still goes through, and saves without a number
    # (older clients) keep working.
    assert client.put(url, json={"response": "Paris", "client_seq": 300}, headers=as_user(user)).json()["response"] == "Paris"
    assert client.put(url, json={"response": "Rome"}, headers=as_user(user)).json()["response"] == "Rome"


def test_writing_and_speaking_saves_are_ordered_too(client, session):
    user, attempt_id, content = _start(client, session, "writing")
    prompt_id = content["sections"][0]["writing_prompts"][0]["id"]
    url = f"/api/v1/attempts/{attempt_id}/writing/{prompt_id}"
    client.put(url, json={"body": "full essay text", "client_seq": 20}, headers=as_user(user))
    late = client.put(url, json={"body": "full", "client_seq": 10}, headers=as_user(user)).json()
    assert late["body"] == "full essay text"

    user, attempt_id, content = _start(client, session, "speaking")
    prompt_id = content["sections"][0]["speaking_prompts"][0]["id"]
    url = f"/api/v1/attempts/{attempt_id}/speaking/{prompt_id}"
    client.put(url, json={"response_mode": "text", "text_response": "final", "completed": True, "client_seq": 5}, headers=as_user(user))
    late = client.put(url, json={"response_mode": "text", "text_response": "draft", "client_seq": 4}, headers=as_user(user)).json()
    assert late["text_response"] == "final" and late["completed"] is True


# --- expiry in lists ---------------------------------------------------------


def test_history_never_shows_a_timed_out_attempt_as_in_progress(client, session):
    user, attempt_id, _ = _start(client, session)
    row = session.get(models.Attempt, UUID(attempt_id))
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.add(row)
    session.commit()

    history = client.get("/api/v1/me/attempts?status=in_progress", headers=as_user(user)).json()
    assert history["total"] == 0
    history = client.get("/api/v1/me/attempts", headers=as_user(user)).json()
    assert history["items"][0]["status"] == "expired"


# --- conflicts -----------------------------------------------------------------


def test_a_database_conflict_is_a_retryable_409_not_a_500():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def boom():
        raise IntegrityError("insert", {}, Exception("duplicate key value violates unique constraint"))

    response = TestClient(app).get("/boom")
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "conflict" and body["retryable"] is True
    assert "duplicate key" not in response.text


# --- browser reports -------------------------------------------------------


def test_browser_reports_are_logged_without_query_strings(client, caplog):
    caplog.set_level("INFO", logger="prepband.client")
    sent = client.post(
        "/api/v1/client-events",
        json={"kind": "error", "message": "TypeError: x is undefined", "page": "/auth/reset?token=secret#x", "release": "abc123"},
    )
    assert sent.status_code == 204
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "TypeError" in logged and "/auth/reset" in logged
    assert "secret" not in logged


def test_browser_reports_are_validated_and_rate_limited(client):
    assert client.post("/api/v1/client-events", json={"kind": "nonsense", "message": "x"}).status_code == 422
    assert client.post("/api/v1/client-events", json={"kind": "error", "message": "x" * 501}).status_code == 422
    codes = [client.post("/api/v1/client-events", json={"kind": "error", "message": "x"}).status_code for _ in range(21)]
    assert codes[-1] == 429


# --- frozen per-question marks ------------------------------------------------


def test_results_use_the_marks_frozen_at_submit(client, session):
    user, attempt_id, content = _start(client, session)
    first, second = (q["id"] for q in content["sections"][0]["questions"])
    client.put(f"/api/v1/attempts/{attempt_id}/responses/{first}", json={"response": "London"}, headers=as_user(user))
    submitted = client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=as_user(user)).json()
    assert submitted["raw_score"] == 1

    row = session.get(models.Attempt, UUID(attempt_id))
    assert row.question_marks == {first: 1.0, second: 0.0}

    # The marking changes later (here: the key itself). Old results must
    # still match the score the student was given.
    key = session.exec(select(models.AnswerKey).where(models.AnswerKey.question_id == UUID(first))).one()
    key.correct_answers = ["Paris"]
    session.add(key)
    session.commit()

    results = client.get(f"/api/v1/attempts/{attempt_id}/results", headers=as_user(user)).json()
    by_id = {q["question_id"]: q for q in results["questions"]}
    assert by_id[first]["marks_awarded"] == 1 and by_id[first]["is_correct"] is True
    assert sum(q["marks_awarded"] for q in results["questions"]) == results["raw_score"]

"""Review my mistakes: which questions are listed, retries marked on the
server, and nobody can use it to read keys of tests they haven't taken."""

from uuid import uuid4

from .conftest import as_user, make_user, publish_test


def _finish(client, user, test, answers):
    attempt = client.post("/api/v1/attempts", json={"test_id": str(test.id)}, headers=as_user(user)).json()
    content = client.get(f"/api/v1/attempts/{attempt['id']}/content", headers=as_user(user)).json()
    ids = [q["id"] for q in content["sections"][0]["questions"]]
    for qid, answer in zip(ids, answers):
        if answer is not None:
            client.put(f"/api/v1/attempts/{attempt['id']}/responses/{qid}", json={"response": answer}, headers=as_user(user))
    assert client.post(f"/api/v1/attempts/{attempt['id']}/submit", headers=as_user(user)).status_code == 200
    return attempt["id"], ids


def test_wrong_and_skipped_questions_are_listed_with_their_answers(client, session):
    user = make_user(session)
    test = publish_test(session, "reading", "academic", kind="practice")
    _, (q1, q2) = _finish(client, user, test, ["London", None])  # Q1 right, Q2 skipped

    page = client.get("/api/v1/me/mistakes", headers=as_user(user)).json()
    assert page["total"] == 1 and page["by_module"] == {"listening": 0, "reading": 1}
    item = page["items"][0]
    assert item["question_id"] == q2
    assert item["your_answer"] is None and item["correct_answers"] == ["Tuesday"]
    assert item["module"] == "reading" and item["marks_awarded"] == 0


def test_getting_it_right_later_takes_it_off_the_list(client, session):
    user = make_user(session)
    test = publish_test(session, "reading", "academic", kind="practice")
    _finish(client, user, test, ["Paris", "Monday"])
    assert client.get("/api/v1/me/mistakes", headers=as_user(user)).json()["total"] == 2
    _finish(client, user, test, ["London", "Monday"])
    page = client.get("/api/v1/me/mistakes", headers=as_user(user)).json()
    assert page["total"] == 1 and page["items"][0]["correct_answers"] == ["Tuesday"]


def test_module_filter_and_writing_is_never_included(client, session):
    user = make_user(session)
    _finish(client, user, publish_test(session, "listening", "both", kind="practice"), [None, None])
    _finish(client, user, publish_test(session, "reading", "academic", kind="practice"), [None, None])
    writing = publish_test(session, "writing", "academic", kind="practice")
    attempt = client.post("/api/v1/attempts", json={"test_id": str(writing.id)}, headers=as_user(user)).json()
    client.post(f"/api/v1/attempts/{attempt['id']}/submit", headers=as_user(user))

    everything = client.get("/api/v1/me/mistakes", headers=as_user(user)).json()
    assert everything["total"] == 4 and everything["by_module"] == {"listening": 2, "reading": 2}
    listening = client.get("/api/v1/me/mistakes?module=listening", headers=as_user(user)).json()
    assert {i["module"] for i in listening["items"]} == {"listening"}
    assert client.get("/api/v1/me/mistakes?module=writing", headers=as_user(user)).status_code == 422


def test_an_unfinished_attempt_reveals_nothing(client, session):
    user = make_user(session)
    test = publish_test(session, "reading", "academic", kind="practice")
    attempt = client.post("/api/v1/attempts", json={"test_id": str(test.id)}, headers=as_user(user)).json()
    content = client.get(f"/api/v1/attempts/{attempt['id']}/content", headers=as_user(user)).json()
    qid = content["sections"][0]["questions"][0]["id"]
    assert client.get("/api/v1/me/mistakes", headers=as_user(user)).json()["total"] == 0
    # Can't check answers of a test still in progress.
    assert client.post(f"/api/v1/me/mistakes/{qid}/check", json={"response": "London"}, headers=as_user(user)).status_code == 404


def test_retries_are_marked_by_the_real_grader(client, session):
    user = make_user(session)
    test = publish_test(session, "reading", "academic", kind="practice")
    _, (q1, _) = _finish(client, user, test, ["Paris", None])

    wrong = client.post(f"/api/v1/me/mistakes/{q1}/check", json={"response": "Rome"}, headers=as_user(user)).json()
    assert wrong["correct"] is False and wrong["credit"] == 0 and wrong["correct_answers"] == ["London"]
    right = client.post(f"/api/v1/me/mistakes/{q1}/check", json={"response": " london. "}, headers=as_user(user)).json()
    assert right["correct"] is True and right["credit"] == 1


def test_other_students_and_strangers_get_nothing(client, session):
    alice, bob = make_user(session, "Alice"), make_user(session, "Bob")
    test = publish_test(session, "reading", "academic", kind="practice")
    _, (q1, _) = _finish(client, alice, test, [None, None])

    assert client.get("/api/v1/me/mistakes", headers=as_user(bob)).json()["total"] == 0
    # Bob never took the test: no answer key through the retry checker.
    assert client.post(f"/api/v1/me/mistakes/{q1}/check", json={"response": "x"}, headers=as_user(bob)).status_code == 404
    assert client.post(f"/api/v1/me/mistakes/{uuid4()}/check", json={"response": "x"}, headers=as_user(alice)).status_code == 404
    assert client.get("/api/v1/me/mistakes").status_code == 401

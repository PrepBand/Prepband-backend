"""Phase 4 - full mock orchestration, consolidated results, history and
progress (FR-MOCK-*, FR-RESULT-*; acceptance scenario 9)."""

from datetime import datetime, timedelta, timezone

from app.models import Attempt
from app.schemas.mocks import MockModuleResult
from app.services.mock_flow import overall_band

from .conftest import as_user, make_user, publish_full_mock, publish_test, uid


def _content(client, user, attempt_id):
    response = client.get(f"/api/v1/attempts/{attempt_id}/content", headers=as_user(user))
    assert response.status_code == 200, response.text
    return response.json()


def _start_module(client, user, mock_id, module):
    response = client.post(f"/api/v1/mocks/{mock_id}/modules/{module}/start", headers=as_user(user))
    assert response.status_code == 200, response.text
    return response.json()


def _finish_module(client, user, mock_id, module, answer_first_correctly=True):
    started = _start_module(client, user, mock_id, module)
    attempt_id = started["attempt_id"]
    content = _content(client, user, attempt_id)
    section = content["sections"][0]
    if module in ("listening", "reading"):
        q1 = section["questions"][0]["id"]
        value = "london" if answer_first_correctly else "Paris"
        assert client.put(f"/api/v1/attempts/{attempt_id}/responses/{q1}", json={"response": value}, headers=as_user(user)).status_code == 200
    elif module == "writing":
        prompt_id = section["writing_prompts"][0]["id"]
        body = "Overall, the chart shows a clear rise. " * 20
        assert client.put(f"/api/v1/attempts/{attempt_id}/writing/{prompt_id}", json={"body": body}, headers=as_user(user)).status_code == 200
    else:
        for prompt in section["speaking_prompts"][:2]:
            payload = {"response_mode": "audio", "duration_seconds": 42, "completed": True}
            assert client.put(f"/api/v1/attempts/{attempt_id}/speaking/{prompt['id']}", json=payload, headers=as_user(user)).status_code == 200
    submitted = client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=as_user(user))
    assert submitted.status_code == 200
    assert submitted.json()["mode"] == "exam"
    return attempt_id


def test_start_reports_every_missing_module(client, session):
    user = make_user(session)
    publish_test(session, "reading", "academic")
    response = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user))
    assert response.status_code == 409
    details = " ".join(d["msg"] for d in response.json()["error"]["details"])
    assert "Listening" in details and "Writing" in details and "Speaking" in details
    assert "Reading" not in details


def test_practice_tests_are_never_used_in_a_full_mock(client, session):
    user = make_user(session)
    for module in ("listening", "reading", "writing", "speaking"):
        publish_test(session, module, "academic", kind="practice")
    assert client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).status_code == 409


def test_track_selection_uses_matching_or_shared_tests(client, session):
    user = make_user(session)
    tests = publish_full_mock(session, "academic")
    publish_test(session, "reading", "general")
    mock = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    by_module = {m["module"]: m for m in mock["modules"]}
    assert by_module["reading"]["test_id"] == str(tests["reading"].id)
    assert by_module["listening"]["test_id"] == str(tests["listening"].id)  # shared "both"


def test_full_mock_end_to_end(client, session):
    user = make_user(session)
    publish_full_mock(session)

    mock = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    assert mock["status"] == "in_progress"
    assert mock["next_module"] == "listening"
    assert [m["module"] for m in mock["modules"]] == ["listening", "reading", "writing", "speaking"]
    assert all(m["status"] == "not_started" for m in mock["modules"])

    # Starting again resumes the same mock (FR-MOCK-04).
    again = client.post("/api/v1/mocks", json={"track": "general"}, headers=as_user(user)).json()
    assert again["id"] == mock["id"]

    # Guided order (FR-MOCK-02).
    out_of_order = client.post(f"/api/v1/mocks/{mock['id']}/modules/reading/start", headers=as_user(user))
    assert out_of_order.status_code == 409
    assert "Finish Listening" in out_of_order.json()["error"]["message"]

    # Starting a module twice returns the same attempt, never a duplicate.
    first = _start_module(client, user, mock["id"], "listening")
    second = _start_module(client, user, mock["id"], "listening")
    assert first["attempt_id"] == second["attempt_id"]
    progress = client.get(f"/api/v1/mocks/{mock['id']}", headers=as_user(user)).json()
    assert progress["modules"][0]["status"] == "in_progress"
    assert progress["completed_modules"] == 0

    # Results are blocked until the mock is complete.
    assert client.get(f"/api/v1/mocks/{mock['id']}/results", headers=as_user(user)).status_code == 409
    early = client.post(f"/api/v1/mocks/{mock['id']}/complete", headers=as_user(user))
    assert early.status_code == 409
    assert len(early.json()["error"]["details"]) == 4

    for module in ("listening", "reading", "writing", "speaking"):
        _finish_module(client, user, mock["id"], module)

    # Reading the mock after the last module completes it (refresh-safe).
    done = client.get(f"/api/v1/mocks/{mock['id']}", headers=as_user(user)).json()
    assert done["status"] == "completed"
    assert done["completed_modules"] == 4
    assert done["next_module"] is None
    assert done["certificate"]["code"].startswith("PB-")
    assert done["certificate"]["public_verification"] is False

    # Completing again is idempotent and never issues a second certificate.
    completed = client.post(f"/api/v1/mocks/{mock['id']}/complete", headers=as_user(user)).json()
    assert completed["certificate"]["id"] == done["certificate"]["id"]
    certs = client.get("/api/v1/certificates", headers=as_user(user)).json()
    assert len(certs) == 1

    results = client.get(f"/api/v1/mocks/{mock['id']}/results", headers=as_user(user)).json()
    by_module = {m["module"]: m for m in results["modules"]}
    assert by_module["listening"]["raw_score"] == 1 and by_module["listening"]["max_score"] == 2
    assert by_module["listening"]["estimated_band"] == 4.5
    assert by_module["listening"]["assessment_status"] == "objective"
    assert by_module["writing"]["assessment_status"] == "not_assessed"
    assert by_module["writing"]["estimated_band"] is None
    assert by_module["writing"]["tasks_answered"] == 1 and by_module["writing"]["tasks_total"] == 2
    assert by_module["writing"]["words_written"] > 100
    assert by_module["speaking"]["speaking_answers"] == 2 and by_module["speaking"]["speaking_prompts_total"] == 3
    # FR-MOCK-06: never an overall band while Writing/Speaking are unassessed.
    assert results["overall_band"] is None
    assert results["overall_band_note"].startswith("Overall band: Not available.")

    # A new mock can be started once the old one is complete.
    fresh = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    assert fresh["id"] != mock["id"]
    assert len(client.get("/api/v1/mocks", headers=as_user(user)).json()) == 2


def test_mock_module_attempts_are_separate_from_standalone_attempts(client, session):
    user = make_user(session)
    tests = publish_full_mock(session)
    mock = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    in_mock = _start_module(client, user, mock["id"], "listening")

    standalone = client.post("/api/v1/attempts", json={"test_id": str(tests["listening"].id), "mode": "exam"}, headers=as_user(user)).json()
    assert standalone["id"] != in_mock["attempt_id"]
    # ...and the standalone one resumes as usual.
    again = client.post("/api/v1/attempts", json={"test_id": str(tests["listening"].id), "mode": "exam"}, headers=as_user(user)).json()
    assert again["id"] == standalone["id"]


def test_expired_module_counts_as_finished(client, session):
    user = make_user(session)
    publish_full_mock(session)
    mock = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    started = _start_module(client, user, mock["id"], "listening")

    attempt = session.get(Attempt, uid(started["attempt_id"]))
    attempt.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.add(attempt)
    session.commit()

    progress = client.get(f"/api/v1/mocks/{mock['id']}", headers=as_user(user)).json()
    assert progress["modules"][0]["status"] == "expired"
    assert progress["next_module"] == "reading"
    assert _start_module(client, user, mock["id"], "reading")["status"] == "in_progress"


def test_abandon_cancels_open_module_and_allows_a_new_mock(client, session):
    user = make_user(session)
    publish_full_mock(session)
    mock = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    started = _start_module(client, user, mock["id"], "listening")

    abandoned = client.post(f"/api/v1/mocks/{mock['id']}/abandon", headers=as_user(user)).json()
    assert abandoned["status"] == "abandoned"
    assert abandoned["modules"][0]["status"] == "cancelled"
    assert abandoned["certificate"] is None
    # The cancelled attempt takes no more answers.
    content = _content(client, user, started["attempt_id"])
    q1 = content["sections"][0]["questions"][0]["id"]
    assert client.put(f"/api/v1/attempts/{started['attempt_id']}/responses/{q1}", json={"response": "x"}, headers=as_user(user)).status_code == 409
    # No further modules, no completion.
    assert client.post(f"/api/v1/mocks/{mock['id']}/modules/reading/start", headers=as_user(user)).status_code == 409
    assert client.post(f"/api/v1/mocks/{mock['id']}/complete", headers=as_user(user)).status_code == 409

    fresh = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    assert fresh["id"] != mock["id"] and fresh["status"] == "in_progress"


def test_completed_mock_cannot_be_abandoned(client, session):
    user = make_user(session)
    publish_full_mock(session)
    mock = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    for module in ("listening", "reading", "writing", "speaking"):
        _finish_module(client, user, mock["id"], module)
    assert client.post(f"/api/v1/mocks/{mock['id']}/abandon", headers=as_user(user)).status_code == 409


def test_other_students_cannot_see_or_touch_a_mock(client, session):
    owner = make_user(session)
    intruder = make_user(session, "Someone Else")
    publish_full_mock(session)
    mock = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(owner)).json()
    started = _start_module(client, owner, mock["id"], "listening")

    for method, path in [
        ("get", f"/api/v1/mocks/{mock['id']}"),
        ("get", f"/api/v1/mocks/{mock['id']}/results"),
        ("post", f"/api/v1/mocks/{mock['id']}/modules/listening/start"),
        ("post", f"/api/v1/mocks/{mock['id']}/complete"),
        ("post", f"/api/v1/mocks/{mock['id']}/abandon"),
        ("get", f"/api/v1/attempts/{started['attempt_id']}/content"),
    ]:
        assert getattr(client, method)(path, headers=as_user(intruder)).status_code == 404, path
    assert client.get("/api/v1/mocks", headers=as_user(intruder)).json() == []
    assert client.get(f"/api/v1/mocks/{mock['id']}").status_code == 401


def test_attempt_content_stays_pinned_after_test_is_archived(client, session):
    user = make_user(session)
    tests = publish_full_mock(session)
    mock = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    started = _start_module(client, user, mock["id"], "listening")

    test = tests["listening"]
    test.status = "archived"
    session.add(test)
    session.commit()

    assert client.get(f"/api/v1/tests/{test.id}", headers=as_user(user)).status_code == 404
    content = _content(client, user, started["attempt_id"])
    assert len(content["sections"][0]["questions"]) == 2
    # Content never leaks answer keys.
    assert "correct_answers" not in str(content)


def test_history_filters_and_pagination(client, session):
    user = make_user(session)
    tests = publish_full_mock(session)
    practice = publish_test(session, "reading", "academic", kind="practice")
    mock = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    _finish_module(client, user, mock["id"], "listening")
    attempt = client.post("/api/v1/attempts", json={"test_id": str(practice.id)}, headers=as_user(user)).json()
    client.post(f"/api/v1/attempts/{attempt['id']}/submit", headers=as_user(user))
    client.post("/api/v1/attempts", json={"test_id": str(tests["reading"].id), "mode": "exam"}, headers=as_user(user))

    page = client.get("/api/v1/me/attempts", headers=as_user(user)).json()
    assert page["total"] == 3 and len(page["items"]) == 3

    first = client.get("/api/v1/me/attempts?limit=2&offset=0", headers=as_user(user)).json()
    second = client.get("/api/v1/me/attempts?limit=2&offset=2", headers=as_user(user)).json()
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})

    full_mock = client.get("/api/v1/me/attempts?kind=full_mock", headers=as_user(user)).json()
    assert full_mock["total"] == 1 and full_mock["items"][0]["mock_attempt_id"] == mock["id"]
    assert client.get("/api/v1/me/attempts?kind=practice", headers=as_user(user)).json()["total"] == 1
    assert client.get("/api/v1/me/attempts?module=reading", headers=as_user(user)).json()["total"] == 2
    assert client.get("/api/v1/me/attempts?status=in_progress", headers=as_user(user)).json()["total"] == 1
    assert client.get("/api/v1/me/attempts?track=both", headers=as_user(user)).json()["total"] == 1
    today = datetime.now(timezone.utc).date()
    assert client.get(f"/api/v1/me/attempts?from={today}&to={today}", headers=as_user(user)).json()["total"] == 3
    assert client.get(f"/api/v1/me/attempts?to={today - timedelta(days=1)}", headers=as_user(user)).json()["total"] == 0
    assert client.get("/api/v1/me/attempts?status=bogus", headers=as_user(user)).status_code == 422
    assert client.get("/api/v1/me/attempts?limit=500", headers=as_user(user)).status_code == 422

    other = make_user(session, "Other")
    assert client.get("/api/v1/me/attempts", headers=as_user(other)).json()["total"] == 0


def test_progress_counts_only_finished_attempts(client, session):
    user = make_user(session)
    publish_full_mock(session)
    mock = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    for module in ("listening", "reading", "writing", "speaking"):
        _finish_module(client, user, mock["id"], module)
    client.get(f"/api/v1/mocks/{mock['id']}", headers=as_user(user))
    second = client.post("/api/v1/mocks", json={"track": "academic"}, headers=as_user(user)).json()
    _start_module(client, user, second["id"], "listening")  # still open: not counted

    progress = client.get("/api/v1/me/progress", headers=as_user(user)).json()
    assert progress["completed_by_module"] == {"listening": 1, "reading": 1, "writing": 1, "speaking": 1}
    assert progress["full_mocks_completed"] == 1
    assert progress["certificates"] == 1
    listening = next(p for p in progress["points"] if p["module"] == "listening")
    assert listening["percentage"] == 50.0 and listening["estimated_band"] == 4.5
    writing = next(p for p in progress["points"] if p["module"] == "writing")
    assert writing["assessment_status"] == "not_assessed" and writing["estimated_band"] is None
    assert writing["words_written"] > 100


def test_overall_band_rules():
    def result(module, band):
        return MockModuleResult(
            module=module, test_title="t", attempt_id=None, status="submitted", mode="exam",
            assessment_status="objective", raw_score=None, max_score=None, estimated_band=band, submitted_at=None,
        )

    band, note = overall_band([result("listening", 7), result("reading", 6.5), result("writing", None), result("speaking", None)])
    assert band is None and "Writing and Speaking are not assessed" in note
    band, note = overall_band([result("listening", None), result("reading", 6.5), result("writing", None), result("speaking", None)])
    assert band is None and "Listening, Writing, Speaking have no band" in note
    # Mean 6.125 -> 6.0, 6.25 -> 6.5, 6.75 -> 7.0 (IELTS-style rounding).
    assert overall_band([result(m, b) for m, b in zip(["listening", "reading", "writing", "speaking"], [6.5, 6, 6, 6])])[0] == 6.0
    assert overall_band([result(m, b) for m, b in zip(["listening", "reading", "writing", "speaking"], [6.5, 6.5, 6, 6])])[0] == 6.5
    assert overall_band([result(m, b) for m, b in zip(["listening", "reading", "writing", "speaking"], [7, 7, 6.5, 6.5])])[0] == 7.0

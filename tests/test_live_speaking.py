"""Phase 5 - live speaking examiner: follow-ups and examiner audio on
prompts, consent-gated live transcripts, and the examiner phrase bank
(FR-LIVE-07/09/11)."""

from datetime import datetime, timezone

from sqlmodel import select

from app.models import ExaminerPhrase, SpeakingPrompt

from .conftest import as_user, make_user, publish_test

DEFAULT_PHRASES = {"greeting": "Hello.", "ack_1": "Mm-hm.", "end": "That is the end of the speaking test."}


def _seed_phrases(session):
    now = datetime.now(timezone.utc)
    for key, text in DEFAULT_PHRASES.items():
        session.add(ExaminerPhrase(key=key, text=text, updated_at=now))
    session.commit()


def _speaking_attempt(client, session, user):
    test = publish_test(session, "speaking", "both", kind="practice")
    prompt = session.exec(select(SpeakingPrompt).where(SpeakingPrompt.part == 1)).first()
    prompt.follow_ups = [{"text": "Why is that?", "audio_url": None}, {"text": "Do you still do that?", "audio_url": "https://cdn.example/a.wav"}]
    prompt.audio_url = "https://cdn.example/q.wav"
    session.add(prompt)
    session.commit()
    attempt = client.post("/api/v1/attempts", json={"test_id": str(test.id), "mode": "exam"}, headers=as_user(user)).json()
    return test, prompt, attempt


LIVE_ANSWER = {
    "response_mode": "live",
    "duration_seconds": 41.5,
    "completed": True,
    "transcript": "I live in Lahore. It is a big city.",
    "stt_model": "whisper-base.en",
    "turns": [
        {"kind": "question", "text": "Where do you live?", "duration_seconds": 30, "long_pauses": 1, "ended_by": "silence",
         "transcript": "I live in Lahore.", "stt_model": "whisper-base.en"},
        {"kind": "follow_up", "text": "Why is that?", "duration_seconds": 11.5, "long_pauses": 0, "ended_by": "time",
         "transcript": "It is a big city.", "stt_model": "whisper-base.en"},
    ],
}


def test_student_content_carries_follow_ups_and_audio(client, session):
    user = make_user(session)
    test, prompt, attempt = _speaking_attempt(client, session, user)
    content = client.get(f"/api/v1/attempts/{attempt['id']}/content", headers=as_user(user)).json()
    prompts = {p["id"]: p for p in content["sections"][0]["speaking_prompts"]}
    mine = prompts[str(prompt.id)]
    assert mine["audio_url"] == "https://cdn.example/q.wav"
    assert [f["text"] for f in mine["follow_ups"]] == ["Why is that?", "Do you still do that?"]
    other = next(p for p in prompts.values() if p["id"] != str(prompt.id))
    assert other["follow_ups"] == [] and other["audio_url"] is None


def test_live_answer_without_consent_keeps_timings_only(client, session):
    user = make_user(session)
    _, prompt, attempt = _speaking_attempt(client, session, user)
    saved = client.put(f"/api/v1/attempts/{attempt['id']}/speaking/{prompt.id}", json=LIVE_ANSWER, headers=as_user(user))
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["response_mode"] == "live" and body["duration_seconds"] == 41.5
    assert body["transcript"] is None
    assert body["stt_model"] == "whisper-base.en"
    assert [t["transcript"] for t in body["turns"]] == [None, None]
    assert [t["duration_seconds"] for t in body["turns"]] == [30, 11.5]
    assert body["turns"][0]["long_pauses"] == 1


def test_live_answer_with_consent_keeps_transcript_through_results(client, session):
    user = make_user(session)
    _, prompt, attempt = _speaking_attempt(client, session, user)
    payload = LIVE_ANSWER | {"save_transcript": True}
    assert client.put(f"/api/v1/attempts/{attempt['id']}/speaking/{prompt.id}", json=payload, headers=as_user(user)).status_code == 200
    restored = client.get(f"/api/v1/attempts/{attempt['id']}", headers=as_user(user)).json()
    assert restored["speaking_responses"][0]["transcript"].startswith("I live in Lahore")

    client.post(f"/api/v1/attempts/{attempt['id']}/submit", headers=as_user(user))
    results = client.get(f"/api/v1/attempts/{attempt['id']}/results", headers=as_user(user)).json()
    assert results["assessment_status"] == "not_assessed"
    assert results["estimated_band"] is None  # FR-LIVE-10: still no band
    live = next(s for s in results["speaking"] if s["speaking_prompt_id"] == str(prompt.id))
    assert live["response_mode"] == "live"
    assert live["turns"][1]["transcript"] == "It is a big city."


def test_live_answer_validation(client, session):
    user = make_user(session)
    _, prompt, attempt = _speaking_attempt(client, session, user)
    url = f"/api/v1/attempts/{attempt['id']}/speaking/{prompt.id}"
    assert client.put(url, json=LIVE_ANSWER | {"text_response": "typed"}, headers=as_user(user)).status_code == 422
    audio_with_transcript = {"response_mode": "audio", "duration_seconds": 3, "completed": True, "transcript": "x"}
    assert client.put(url, json=audio_with_transcript, headers=as_user(user)).status_code == 422
    too_many = LIVE_ANSWER | {"turns": LIVE_ANSWER["turns"] * 11}
    assert client.put(url, json=too_many, headers=as_user(user)).status_code == 422
    bad_kind = LIVE_ANSWER | {"turns": [LIVE_ANSWER["turns"][0] | {"kind": "score"}]}
    assert client.put(url, json=bad_kind, headers=as_user(user)).status_code == 422
    # Switching back to plain audio clears live data.
    client.put(url, json=LIVE_ANSWER | {"save_transcript": True}, headers=as_user(user))
    plain = client.put(url, json={"response_mode": "audio", "duration_seconds": 5, "completed": True}, headers=as_user(user)).json()
    assert plain["transcript"] is None and plain["turns"] is None


def test_admin_authoring_follow_ups_and_audio_round_trip(client, session):
    admin = make_user(session, "Admin", role="admin")
    created = client.post(
        "/api/v1/admin/tests", json={"title": "Live speaking draft", "module": "speaking", "track": "both", "kind": "practice"},
        headers=as_user(admin),
    )
    assert created.status_code == 201, created.text
    test_id = created.json()["id"]
    section = client.post(f"/api/v1/admin/tests/{test_id}/sections", json={"module": "speaking", "order_index": 0}, headers=as_user(admin))
    assert section.status_code == 201, section.text
    section_id = section.json()["id"]

    prompt = client.post(
        f"/api/v1/admin/sections/{section_id}/speaking-prompts",
        json={"part": 1, "prompt": "Do you work or study?", "display_order": 0, "follow_ups": [{"text": "Why did you choose it?"}]},
        headers=as_user(admin),
    )
    assert prompt.status_code == 201, prompt.text
    assert prompt.json()["follow_ups"] == [{"text": "Why did you choose it?", "audio_url": None}]

    bad = client.patch(f"/api/v1/admin/speaking-prompts/{prompt.json()['id']}", json={"audio_url": "http://insecure/a.wav"}, headers=as_user(admin))
    assert bad.status_code == 422
    eleven = [{"text": f"F{i}"} for i in range(11)]
    assert client.patch(f"/api/v1/admin/speaking-prompts/{prompt.json()['id']}", json={"follow_ups": eleven}, headers=as_user(admin)).status_code == 422

    updated = client.patch(
        f"/api/v1/admin/speaking-prompts/{prompt.json()['id']}",
        json={"audio_url": "https://cdn.example/q.wav", "follow_ups": [{"text": "Why did you choose it?", "audio_url": "https://cdn.example/f.wav"}]},
        headers=as_user(admin),
    )
    assert updated.status_code == 200
    assert updated.json()["audio_url"] == "https://cdn.example/q.wav"

    exported = client.get(f"/api/v1/admin/tests/{test_id}/export", headers=as_user(admin)).json()
    doc_prompt = exported["sections"][0]["speaking_prompts"][0]
    assert doc_prompt["follow_ups"][0]["audio_url"] == "https://cdn.example/f.wav"
    reimported = client.post("/api/v1/admin/tests/import", json=exported, headers=as_user(admin))
    assert reimported.status_code == 201, reimported.text
    tree = client.get(f"/api/v1/admin/tests/{reimported.json()['id']}/content", headers=as_user(admin)).json()
    assert tree["sections"][0]["speaking_prompts"][0]["follow_ups"][0]["text"] == "Why did you choose it?"


def test_examiner_phrase_bank(client, session):
    _seed_phrases(session)
    student = make_user(session)
    admin = make_user(session, "Admin", role="admin")

    phrases = client.get("/api/v1/examiner/phrases", headers=as_user(student)).json()
    assert {p["key"] for p in phrases} == set(DEFAULT_PHRASES)
    assert client.get("/api/v1/examiner/phrases").status_code == 401

    url = "/api/v1/admin/examiner/phrases/greeting"
    assert client.put(url, json={"text": "Hi"}, headers=as_user(student)).status_code == 403
    assert client.put("/api/v1/admin/examiner/phrases/not_a_line", json={"text": "x"}, headers=as_user(admin)).status_code == 404
    assert client.put(url, json={"text": "Hello.", "audio_url": "http://x"}, headers=as_user(admin)).status_code == 422

    with_audio = client.put(url, json={"text": "Hello.", "audio_url": "https://cdn.example/g.wav"}, headers=as_user(admin)).json()
    assert with_audio["audio_url"] == "https://cdn.example/g.wav"
    # Rewording a line while leaving the old clip drops the stale clip.
    reworded = client.put(url, json={"text": "Good morning.", "audio_url": "https://cdn.example/g.wav"}, headers=as_user(admin)).json()
    assert reworded["text"] == "Good morning." and reworded["audio_url"] is None
    admin_view = client.get("/api/v1/admin/examiner/phrases", headers=as_user(admin)).json()
    assert next(p for p in admin_view if p["key"] == "greeting")["text"] == "Good morning."

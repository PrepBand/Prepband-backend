"""Regression: the exact answer payload the live examiner's browser code sends."""

from sqlmodel import select

from app.models import SpeakingPrompt

from .conftest import as_user, make_user, publish_test


def test_browser_live_payload_without_transcript_is_accepted(client, session):
    user = make_user(session)
    test = publish_test(session, "speaking", "both", kind="practice", duration=40)
    prompt = session.exec(select(SpeakingPrompt).where(SpeakingPrompt.part == 1)).first()
    attempt = client.post("/api/v1/attempts", json={"test_id": str(test.id), "mode": "exam"}, headers=as_user(user)).json()
    payload = {
        "response_mode": "live",
        "duration_seconds": 12.6,
        "text_response": None,
        "completed": True,
        "save_transcript": False,
        "transcript": None,
        "stt_model": None,
        "turns": [
            {"kind": "question", "text": "Do you live in a house or an apartment?", "duration_seconds": 6.3, "long_pauses": 0,
             "ended_by": "silence", "transcript": None, "stt_model": None},
            {"kind": "follow_up", "text": "What do you like most about it?", "duration_seconds": 6.3, "long_pauses": 0,
             "ended_by": "silence", "transcript": None, "stt_model": None},
        ],
    }
    response = client.put(f"/api/v1/attempts/{attempt['id']}/speaking/{prompt.id}", json=payload, headers=as_user(user))
    assert response.status_code == 200, response.text

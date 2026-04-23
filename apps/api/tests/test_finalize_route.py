from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from models import ActionItem, Meeting, MeetingEvent, Summary, TranscriptUtterance


def _client():
    from main import app
    return TestClient(app)


def _meeting(db, status: str = "in_call") -> Meeting:
    m = Meeting(
        title="Phase 7 test", meeting_url="https://meet.google.com/x",
        status=status, recall_bot_id=f"bot_{uuid.uuid4().hex[:8]}",
    )
    db.add(m); db.commit(); db.refresh(m)
    return m


def _utt(db, meeting: Meeting, text: str, start_ms: int) -> None:
    db.add(TranscriptUtterance(
        meeting_id=meeting.id, text=text, speaker_label="customer",
        is_partial=False, start_ms=start_ms, end_ms=start_ms + 500,
    ))
    db.commit()


def test_finalize_flips_status_to_processing(db, monkeypatch):
    import meetings as meetings_module

    captured = {}

    async def fake_run_synthesis(meeting_id):
        captured["meeting_id"] = meeting_id

    monkeypatch.setattr(meetings_module, "_run_synthesis", fake_run_synthesis)

    m = _meeting(db)
    _utt(db, m, "We need Salesforce sync", 0)
    r = _client().post(f"/meetings/{m.id}/finalize")
    assert r.status_code == 202
    assert r.json()["status"] == "processing"
    assert captured["meeting_id"] == m.id

    db.expire_all()
    refreshed = db.get(Meeting, m.id)
    assert refreshed.status == "processing"
    assert refreshed.ended_at is not None


def test_finalize_is_idempotent_when_terminal(db):
    m = _meeting(db, status="done")
    r = _client().post(f"/meetings/{m.id}/finalize")
    assert r.status_code == 202
    assert "already terminal" in r.json().get("note", "")


def test_finalize_404(db):
    r = _client().post(f"/meetings/{uuid.uuid4()}/finalize")
    assert r.status_code == 404


def test_crm_push_logs_internal_event(db):
    m = _meeting(db, status="done")
    db.add(Summary(
        meeting_id=m.id, summary_type="crm_note",
        content_markdown="Customer wants SF sync. Send proposal Friday.",
    ))
    db.commit()

    r = _client().post(f"/meetings/{m.id}/crm-push")
    assert r.status_code == 201

    event = db.execute(
        select(MeetingEvent).where(
            MeetingEvent.meeting_id == m.id,
            MeetingEvent.event_type == "internal.crm_pushed",
        )
    ).scalars().first()
    assert event is not None
    assert event.payload_json["destination"] == "mock-crm"
    assert "SF sync" in event.payload_json["note"]

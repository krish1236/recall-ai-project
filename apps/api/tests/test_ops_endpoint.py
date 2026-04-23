from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from models import (
    DeadLetterJob,
    Meeting,
    MeetingEvent,
    TranscriptUtterance,
    UtteranceSpan,
    WebhookDelivery,
)


def _client():
    from main import app
    return TestClient(app)


def _meeting(db) -> Meeting:
    m = Meeting(
        title="Ops test", meeting_url="https://meet.google.com/x",
        status="done", recall_bot_id=f"bot_{uuid.uuid4().hex[:8]}",
    )
    db.add(m); db.commit(); db.refresh(m)
    return m


def test_ops_empty_meeting(db):
    m = _meeting(db)
    r = _client().get(f"/meetings/{m.id}/ops")
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"]["events_accepted"] == 0
    assert body["metrics"]["utterance_count"] == 0
    assert body["events"] == []
    assert body["deliveries"] == []


def test_ops_populates_all_sections(db):
    m = _meeting(db)
    now = datetime.now(tz=timezone.utc)

    evt = MeetingEvent(
        meeting_id=m.id, source="recall", event_type="transcript.data",
        event_timestamp=now, received_at=now,
        payload_json={"data": {"words": [{"text": "hi"}]}},
        dedupe_key=uuid.uuid4().hex, signature_valid=True,
    )
    db.add(evt); db.commit(); db.refresh(evt)

    for i in range(3):
        db.add(WebhookDelivery(
            meeting_id=m.id, event_type="transcript.data",
            headers_json={}, signature_valid=True,
            response_code=200, attempt_count=1,
        ))
    db.add(WebhookDelivery(
        meeting_id=m.id, event_type=None,
        headers_json={}, signature_valid=False,
        response_code=401, attempt_count=1,
    ))
    db.commit()

    u = TranscriptUtterance(
        meeting_id=m.id, source_event_id=evt.id, text="hi",
        speaker_label="rep", is_partial=False, start_ms=0, end_ms=500,
    )
    db.add(u); db.commit(); db.refresh(u)

    db.add(UtteranceSpan(
        utterance_id=u.id,
        persisted_at=now + timedelta(milliseconds=5),
        enqueued_at=now + timedelta(milliseconds=8),
        classified_at=now + timedelta(milliseconds=200),
        pushed_at=now + timedelta(milliseconds=12),
    ))
    db.commit()

    db.add(DeadLetterJob(
        job_kind="classify", meeting_id=m.id,
        payload_json={}, error="boom", attempt_count=1, status="open",
    ))
    db.commit()

    r = _client().get(f"/meetings/{m.id}/ops")
    assert r.status_code == 200
    body = r.json()

    assert body["metrics"]["events_accepted"] == 1
    assert body["metrics"]["webhook_deliveries_ok"] == 3
    assert body["metrics"]["webhook_deliveries_bad_sig"] == 1
    assert body["metrics"]["duplicates_absorbed"] == 2  # 3 ok - 1 event
    assert body["metrics"]["utterance_count"] == 1
    assert body["metrics"]["p50_end_to_end_ms"] == 12

    assert len(body["events"]) == 1
    assert len(body["deliveries"]) == 4
    assert len(body["utterance_spans"]) == 1
    assert len(body["dlq"]) == 1


def test_ops_404(db):
    r = _client().get(f"/meetings/{uuid.uuid4()}/ops")
    assert r.status_code == 404

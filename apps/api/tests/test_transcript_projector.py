from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from handlers import handle_transcript_data
from models import Meeting, MeetingEvent, TranscriptUtterance


def _now() -> datetime:
    return datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)


def _meeting(db) -> Meeting:
    m = Meeting(
        meeting_url="https://meet.google.com/abc",
        status="in_call",
        recall_bot_id=f"bot_{uuid.uuid4().hex[:8]}",
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _event(meeting_id, payload: dict) -> MeetingEvent:
    return MeetingEvent(
        meeting_id=meeting_id,
        source="recall",
        event_type="transcript.data",
        event_timestamp=_now(),
        received_at=_now(),
        payload_json=payload,
        dedupe_key=uuid.uuid4().hex,
        signature_valid=True,
    )


@pytest.mark.asyncio
async def test_projects_words_into_utterance(db):
    m = _meeting(db)
    evt = _event(m.id, {
        "data": {
            "participant": {"name": "Alice", "id": "p1"},
            "words": [
                {"text": "We", "start": 1.2, "end": 1.4},
                {"text": "need", "start": 1.4, "end": 1.7},
                {"text": "pricing", "start": 1.7, "end": 2.1},
            ],
        }
    })
    db.add(evt)
    db.commit()
    await handle_transcript_data(evt, db)
    db.commit()

    rows = db.execute(select(TranscriptUtterance).where(TranscriptUtterance.meeting_id == m.id)).scalars().all()
    assert len(rows) == 1
    r = rows[0]
    assert r.text == "We need pricing"
    assert r.speaker_label == "Alice"
    assert r.start_ms == 1200
    assert r.end_ms == 2100
    assert r.is_partial is False
    assert r.source_event_id == evt.id


@pytest.mark.asyncio
async def test_speaker_fallback_chain(db):
    m = _meeting(db)

    # participant dict without name → falls back to id
    evt = _event(m.id, {
        "data": {
            "participant": {"id": "p2"},
            "words": [{"text": "hi", "start": 0, "end": 0.1}],
        }
    })
    db.add(evt); db.commit()
    await handle_transcript_data(evt, db); db.commit()

    # no participant at all → use data.speaker
    evt2 = _event(m.id, {
        "data": {
            "speaker": "customer",
            "words": [{"text": "yo", "start": 0, "end": 0.1}],
        }
    })
    db.add(evt2); db.commit()
    await handle_transcript_data(evt2, db); db.commit()

    # no participant, no data.speaker → words[0].speaker
    evt3 = _event(m.id, {
        "data": {
            "words": [{"text": "ok", "start": 0, "end": 0.1, "speaker": "rep"}],
        }
    })
    db.add(evt3); db.commit()
    await handle_transcript_data(evt3, db); db.commit()

    speakers = [r.speaker_label for r in db.execute(
        select(TranscriptUtterance).where(TranscriptUtterance.meeting_id == m.id).order_by(TranscriptUtterance.created_at)
    ).scalars().all()]
    assert speakers == ["p2", "customer", "rep"]


@pytest.mark.asyncio
async def test_empty_words_inserts_nothing(db):
    m = _meeting(db)
    evt = _event(m.id, {"data": {"words": []}})
    db.add(evt); db.commit()
    await handle_transcript_data(evt, db); db.commit()
    count = db.execute(select(TranscriptUtterance).where(TranscriptUtterance.meeting_id == m.id)).scalars().all()
    assert count == []


@pytest.mark.asyncio
async def test_blank_text_inserts_nothing(db):
    m = _meeting(db)
    evt = _event(m.id, {"data": {"words": [{"text": "", "start": 0, "end": 0.1}]}})
    db.add(evt); db.commit()
    await handle_transcript_data(evt, db); db.commit()
    count = db.execute(select(TranscriptUtterance).where(TranscriptUtterance.meeting_id == m.id)).scalars().all()
    assert count == []


@pytest.mark.asyncio
async def test_missing_meeting_id_skipped(db):
    evt = MeetingEvent(
        meeting_id=None,
        source="recall",
        event_type="transcript.data",
        event_timestamp=_now(),
        received_at=_now(),
        payload_json={"data": {"words": [{"text": "hi", "start": 0, "end": 0.1}]}},
        dedupe_key=uuid.uuid4().hex,
        signature_valid=True,
    )
    db.add(evt); db.commit()
    await handle_transcript_data(evt, db)
    db.commit()
    # no row inserted
    rows = db.execute(select(TranscriptUtterance)).scalars().all()
    assert rows == []

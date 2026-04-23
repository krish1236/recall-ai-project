from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from handlers import handle_status_change, normalize_status
from models import Meeting, MeetingEvent


def _base_ts() -> datetime:
    return datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)


def _make_meeting(db, *, status: str = "requested", state_changed_at: datetime | None = None) -> Meeting:
    m = Meeting(
        meeting_url="https://meet.google.com/abc",
        status=status,
        state_changed_at=state_changed_at or _base_ts(),
        recall_bot_id=f"bot_{uuid.uuid4().hex[:8]}",
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _status_event(meeting_id, code: str, ts: datetime) -> MeetingEvent:
    return MeetingEvent(
        meeting_id=meeting_id,
        source="recall",
        event_type="bot.status_change",
        event_timestamp=ts,
        received_at=ts,
        payload_json={"data": {"status": {"code": code}}},
        dedupe_key=uuid.uuid4().hex,
        signature_valid=True,
    )


@pytest.mark.asyncio
async def test_normal_progression(db):
    m = _make_meeting(db)
    base = _base_ts()

    for i, code in enumerate(["ready_to_join", "in_call_recording", "call_ended", "done"]):
        ts = base + timedelta(seconds=i + 1)
        evt = _status_event(m.id, code, ts)
        db.add(evt)
        db.commit()
        await handle_status_change(evt, db)
        db.commit()

    db.refresh(m)
    assert m.status == "done"
    assert m.started_at == base + timedelta(seconds=2)
    assert m.ended_at == base + timedelta(seconds=4)


@pytest.mark.asyncio
async def test_stale_events_rejected(db):
    m = _make_meeting(db, status="in_call", state_changed_at=_base_ts() + timedelta(seconds=10))
    # late `joining` from t=5 arrives after in_call was set at t=10
    evt = _status_event(m.id, "joining_call", _base_ts() + timedelta(seconds=5))
    db.add(evt)
    db.commit()
    await handle_status_change(evt, db)
    db.commit()
    db.refresh(m)
    assert m.status == "in_call", "stale event must not regress state"


@pytest.mark.asyncio
async def test_done_is_terminal(db):
    m = _make_meeting(db, status="done", state_changed_at=_base_ts())
    evt = _status_event(m.id, "in_call_recording", _base_ts() + timedelta(seconds=5))
    db.add(evt)
    db.commit()
    await handle_status_change(evt, db)
    db.commit()
    db.refresh(m)
    assert m.status == "done"


@pytest.mark.asyncio
async def test_unknown_status_code_ignored(db):
    m = _make_meeting(db, status="in_call", state_changed_at=_base_ts())
    evt = _status_event(m.id, "bogus_status_xyz", _base_ts() + timedelta(seconds=5))
    db.add(evt)
    db.commit()
    await handle_status_change(evt, db)
    db.commit()
    db.refresh(m)
    assert m.status == "in_call"


@pytest.mark.asyncio
async def test_idempotent_same_status(db):
    m = _make_meeting(db, status="in_call", state_changed_at=_base_ts())
    before = m.state_changed_at
    evt = _status_event(m.id, "in_call_recording", _base_ts() + timedelta(seconds=5))
    db.add(evt)
    db.commit()
    await handle_status_change(evt, db)
    db.commit()
    db.refresh(m)
    assert m.status == "in_call"
    # idempotent no-op: state_changed_at should not move forward
    assert m.state_changed_at == before


@pytest.mark.asyncio
async def test_fatal_goes_to_failed(db):
    m = _make_meeting(db, status="joining", state_changed_at=_base_ts())
    evt = _status_event(m.id, "fatal", _base_ts() + timedelta(seconds=2))
    db.add(evt)
    db.commit()
    await handle_status_change(evt, db)
    db.commit()
    db.refresh(m)
    assert m.status == "failed"
    assert m.ended_at == _base_ts() + timedelta(seconds=2)


def test_status_map_coverage():
    # a handful of sanity checks on the mapping itself
    assert normalize_status("bot_requested") == "requested"
    assert normalize_status("in_call_recording") == "in_call"
    assert normalize_status("call_ended") == "processing"
    assert normalize_status("done") == "done"
    assert normalize_status("fatal") == "failed"
    assert normalize_status("something_else") is None

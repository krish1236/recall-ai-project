from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from models import Insight, InsightEvidence, Meeting, TranscriptUtterance


def _client():
    from main import app
    return TestClient(app)


def _meeting(db, title: str, status: str = "in_call") -> Meeting:
    m = Meeting(
        title=title, meeting_url="https://meet.google.com/x",
        status=status, recall_bot_id=f"bot_{uuid.uuid4().hex[:8]}",
    )
    db.add(m); db.commit(); db.refresh(m)
    return m


def test_list_meetings_empty(db):
    r = _client().get("/meetings")
    assert r.status_code == 200
    assert r.json() == []


def test_list_meetings_returns_rows_with_insight_meta(db):
    m1 = _meeting(db, "Acme Q1")
    m2 = _meeting(db, "Beta Co")

    u = TranscriptUtterance(meeting_id=m1.id, text="x", speaker_label="rep", is_partial=False, start_ms=0, end_ms=100)
    db.add(u); db.commit(); db.refresh(u)
    ins = Insight(meeting_id=m1.id, type="objection", title="Pricing concern", severity="high", confidence=0.9)
    db.add(ins); db.commit(); db.refresh(ins)
    db.add(InsightEvidence(insight_id=ins.id, utterance_id=u.id, evidence_text="x"))
    db.commit()

    r = _client().get("/meetings")
    assert r.status_code == 200
    rows = {row["title"]: row for row in r.json()}
    assert rows["Acme Q1"]["insight_count"] == 1
    assert rows["Acme Q1"]["top_insight_title"] == "Pricing concern"
    assert rows["Acme Q1"]["has_high_severity"] is True
    assert rows["Beta Co"]["insight_count"] == 0
    assert rows["Beta Co"]["has_high_severity"] is False


def test_list_meetings_filters_by_status(db):
    _meeting(db, "live-1", status="in_call")
    _meeting(db, "live-2", status="in_call")
    _meeting(db, "done-1", status="done")

    r = _client().get("/meetings?status=in_call")
    titles = {row["title"] for row in r.json()}
    assert titles == {"live-1", "live-2"}


def test_get_meeting_detail_includes_utterances_and_insights(db):
    m = _meeting(db, "detail-test")
    u1 = TranscriptUtterance(meeting_id=m.id, text="first", is_partial=False, start_ms=0, end_ms=500)
    u2 = TranscriptUtterance(meeting_id=m.id, text="second", is_partial=False, start_ms=500, end_ms=1000)
    db.add_all([u1, u2]); db.commit(); db.refresh(u1); db.refresh(u2)

    ins = Insight(meeting_id=m.id, type="commitment", title="Deal close Friday", severity="medium", confidence=0.8)
    db.add(ins); db.commit(); db.refresh(ins)
    db.add(InsightEvidence(insight_id=ins.id, utterance_id=u2.id, evidence_text="second"))
    db.commit()

    r = _client().get(f"/meetings/{m.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(m.id)
    assert body["status"] == "in_call"
    assert len(body["utterances"]) == 2
    assert body["utterances"][0]["text"] == "first"
    assert len(body["insights"]) == 1
    assert body["insights"][0]["title"] == "Deal close Friday"
    assert body["insights"][0]["evidence_utterance_ids"] == [str(u2.id)]


def test_get_meeting_404(db):
    r = _client().get(f"/meetings/{uuid.uuid4()}")
    assert r.status_code == 404

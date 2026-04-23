from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from models import DeadLetterJob, Meeting


def _client():
    from main import app
    return TestClient(app)


def _meeting(db) -> Meeting:
    m = Meeting(
        status="processing", meeting_url="https://meet.google.com/x",
        recall_bot_id=f"bot_{uuid.uuid4().hex[:8]}",
    )
    db.add(m); db.commit(); db.refresh(m)
    return m


def _dlq(db, *, kind: str, meeting_id, status: str = "open", payload: dict | None = None) -> DeadLetterJob:
    j = DeadLetterJob(
        job_kind=kind, meeting_id=meeting_id,
        payload_json=payload or {}, error="something broke",
        attempt_count=1, status=status,
    )
    db.add(j); db.commit(); db.refresh(j)
    return j


def test_list_empty(db):
    r = _client().get("/admin/dlq")
    assert r.status_code == 200
    assert r.json() == []


def test_list_filters(db):
    m = _meeting(db)
    _dlq(db, kind="classify", meeting_id=m.id, status="open")
    _dlq(db, kind="classify", meeting_id=m.id, status="resolved")
    _dlq(db, kind="synthesize", meeting_id=m.id, status="circuit_open")

    all_rows = _client().get("/admin/dlq").json()
    assert len(all_rows) == 3
    open_rows = _client().get("/admin/dlq?status=open").json()
    assert len(open_rows) == 1
    mid = str(m.id)
    scoped = _client().get(f"/admin/dlq?meeting_id={mid}").json()
    assert len(scoped) == 3


def test_resolve_flips_status(db):
    m = _meeting(db)
    j = _dlq(db, kind="classify", meeting_id=m.id)

    r = _client().post(f"/admin/dlq/{j.id}/resolve")
    assert r.status_code == 200
    assert r.json()["status"] == "resolved"

    db.expire_all()
    refreshed = db.get(DeadLetterJob, j.id)
    assert refreshed.status == "resolved"


def test_resolve_404(db):
    r = _client().post(f"/admin/dlq/{uuid.uuid4()}/resolve")
    assert r.status_code == 404


def test_retry_unsupported_kind(db):
    m = _meeting(db)
    j = _dlq(db, kind="stream_handler:bot.status_change", meeting_id=m.id)
    r = _client().post(f"/admin/dlq/{j.id}/retry")
    assert r.status_code == 200
    assert r.json()["status"] == "unsupported"
    # attempt count incremented
    db.expire_all()
    assert db.get(DeadLetterJob, j.id).attempt_count == 2

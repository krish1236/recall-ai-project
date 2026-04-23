from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meetings import get_recall_client
from models import Meeting
from recall_client import RecallClient


def _fake_recall(response: dict, status: int = 201):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=response)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RecallClient(api_key="tok_test", region="us-east-1", http=http)


def _broken_recall(status: int = 502, body: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body or {"detail": "recall down"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RecallClient(api_key="tok_test", region="us-east-1", http=http)


@pytest.fixture
def client_with_fake_recall():
    from main import app

    def _override():
        return _fake_recall({"id": "bot_fake_xyz", "status": "bot_requested"})

    app.dependency_overrides[get_recall_client] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_recall_client, None)


def test_create_meeting_stores_row_and_bot_id(client_with_fake_recall, db):
    r = client_with_fake_recall.post(
        "/meetings",
        json={
            "meeting_url": "https://meet.google.com/abc-defg-hij",
            "title": "Discovery: Acme",
            "meeting_type": "discovery",
            "owner_name": "rep@acme.com",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["recall_bot_id"] == "bot_fake_xyz"
    assert body["status"] == "joining"

    meeting = db.execute(
        select(Meeting).where(Meeting.id == body["meeting_id"])
    ).scalar_one()
    assert meeting.recall_bot_id == "bot_fake_xyz"
    assert meeting.status == "joining"
    assert meeting.title == "Discovery: Acme"


def test_create_meeting_marks_failed_on_recall_error(db):
    from main import app
    app.dependency_overrides[get_recall_client] = lambda: _broken_recall(status=401)
    try:
        with TestClient(app) as c:
            r = c.post(
                "/meetings",
                json={"meeting_url": "https://meet.google.com/failing"},
            )
    finally:
        app.dependency_overrides.pop(get_recall_client, None)

    assert r.status_code == 502
    meetings = db.execute(select(Meeting)).scalars().all()
    assert len(meetings) == 1
    assert meetings[0].status == "failed"
    assert meetings[0].recall_bot_id is None


def test_get_meeting_returns_current_status(client_with_fake_recall, db):
    r = client_with_fake_recall.post(
        "/meetings",
        json={"meeting_url": "https://meet.google.com/a"},
    )
    assert r.status_code == 201
    mid = r.json()["meeting_id"]

    r2 = client_with_fake_recall.get(f"/meetings/{mid}")
    assert r2.status_code == 200
    assert r2.json()["recall_bot_id"] == "bot_fake_xyz"
    assert r2.json()["status"] == "joining"

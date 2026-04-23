from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from models import MeetingEvent, WebhookDelivery
from tests._helpers import make_payload, svix_headers


def _client():
    from main import app
    return TestClient(app)


def test_replay_50_produces_one_event_and_fifty_deliveries(db):
    c = _client()
    payload = make_payload(
        bot_id="bot_idem_001",
        event="transcript.data",
        timestamp="2026-04-22T10:00:00+00:00",
    )
    raw = json.dumps(payload).encode()
    # Same svix-id reused 50x — models "Recall retried the same delivery 50 times"
    fixed_msg_id = "msg_idempotency_test"
    headers = svix_headers(raw, msg_id=fixed_msg_id)

    outcomes: list[str] = []
    for _ in range(50):
        r = c.post("/webhook/recall", content=raw, headers=headers)
        assert r.status_code == 200, r.text
        outcomes.append(r.json()["status"])

    assert outcomes[0] == "accepted"
    assert outcomes[1:].count("duplicate") == 49

    event_count = db.execute(select(func.count()).select_from(MeetingEvent)).scalar_one()
    delivery_count = db.execute(select(func.count()).select_from(WebhookDelivery)).scalar_one()
    assert event_count == 1
    assert delivery_count == 50


def test_different_payloads_do_not_collide(db):
    c = _client()
    for i in range(5):
        payload = make_payload(
            bot_id="bot_idem_002",
            event="transcript.data",
            timestamp=f"2026-04-22T10:00:0{i}+00:00",
            text=f"utterance {i}",
        )
        raw = json.dumps(payload).encode()
        r = c.post("/webhook/recall", content=raw, headers=svix_headers(raw))
        assert r.status_code == 200
        assert r.json()["status"] == "accepted"

    event_count = db.execute(select(func.count()).select_from(MeetingEvent)).scalar_one()
    assert event_count == 5

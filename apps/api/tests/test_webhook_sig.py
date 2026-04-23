from __future__ import annotations

import json
import os

from fastapi.testclient import TestClient

from tests._helpers import TEST_SECRET, make_payload, svix_headers


def _client():
    os.environ["RECALL_WEBHOOK_SECRET"] = TEST_SECRET
    from main import app
    return TestClient(app)


def test_good_signature_accepted(db):
    c = _client()
    payload = make_payload(bot_id="bot_sig_ok")
    raw = json.dumps(payload).encode()
    r = c.post("/webhook/recall", content=raw, headers=svix_headers(raw))
    assert r.status_code == 200, r.text
    assert r.json()["status"] in {"accepted", "duplicate"}


def test_bad_signature_rejected_and_logged(db):
    from models import WebhookDelivery
    from sqlalchemy import func, select

    c = _client()
    payload = make_payload(bot_id="bot_sig_bad")
    raw = json.dumps(payload).encode()
    before = db.execute(
        select(func.count()).select_from(WebhookDelivery).where(WebhookDelivery.signature_valid.is_(False))
    ).scalar_one()

    r = c.post(
        "/webhook/recall",
        content=raw,
        headers={
            "svix-id": "msg_fake",
            "svix-timestamp": "0",
            "svix-signature": "v1,deadbeef",
            "content-type": "application/json",
        },
    )
    assert r.status_code == 401
    assert r.json() == {"error": "invalid signature"}

    after = db.execute(
        select(func.count()).select_from(WebhookDelivery).where(WebhookDelivery.signature_valid.is_(False))
    ).scalar_one()
    assert after == before + 1


def test_missing_signature_rejected(db):
    c = _client()
    payload = make_payload(bot_id="bot_sig_missing")
    raw = json.dumps(payload).encode()
    r = c.post("/webhook/recall", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 401

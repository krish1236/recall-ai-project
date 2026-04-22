from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db import SessionLocal
from models import Meeting, MeetingEvent, WebhookDelivery

router = APIRouter(tags=["webhook"])


def _webhook_secret() -> str:
    return os.environ.get("RECALL_WEBHOOK_SECRET", "")


def _verify_signature(raw_body: bytes, provided: Optional[str], secret: str) -> bool:
    if not provided or not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    candidate = provided
    if candidate.startswith("sha256="):
        candidate = candidate[len("sha256="):]
    return hmac.compare_digest(expected, candidate)


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _dedupe_key(bot_id: str, event_type: str, event_ts: str, payload: Any) -> str:
    material = f"{bot_id}|{event_type}|{event_ts}|{_canonical_json(payload)}"
    return hashlib.sha256(material.encode()).hexdigest()


def _parse_ts(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


def _extract_envelope(payload: dict) -> tuple[Optional[str], str, datetime]:
    event_type = payload.get("event") or payload.get("event_type") or "unknown"
    data = payload.get("data") or {}
    bot_id = (
        data.get("bot_id")
        or (data.get("bot") or {}).get("id")
        or payload.get("bot_id")
    )
    event_ts = _parse_ts(
        payload.get("timestamp") or data.get("timestamp")
    )
    return bot_id, event_type, event_ts


@router.post("/webhook/recall")
async def ingest(
    request: Request,
    x_recall_signature: Optional[str] = Header(default=None),
):
    raw = await request.body()
    received_at = datetime.now(tz=timezone.utc)
    remote_addr = request.client.host if request.client else None
    headers = {"x-recall-signature": x_recall_signature or ""}

    if not _verify_signature(raw, x_recall_signature, _webhook_secret()):
        with SessionLocal() as s:
            s.add(WebhookDelivery(
                event_type=None,
                headers_json=headers,
                signature_valid=False,
                remote_addr=remote_addr,
                response_code=401,
            ))
            s.commit()
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        with SessionLocal() as s:
            s.add(WebhookDelivery(
                event_type=None,
                headers_json=headers,
                signature_valid=True,
                remote_addr=remote_addr,
                response_code=400,
            ))
            s.commit()
        return JSONResponse({"error": "malformed json"}, status_code=400)

    bot_id, event_type, event_ts = _extract_envelope(payload)

    with SessionLocal() as s:
        meeting_id = None
        if bot_id:
            meeting_id = s.execute(
                select(Meeting.id).where(Meeting.recall_bot_id == bot_id)
            ).scalar_one_or_none()

        outcome = "accepted"
        if bot_id:
            key = _dedupe_key(bot_id, event_type, event_ts.isoformat(), payload)
            stmt = (
                pg_insert(MeetingEvent)
                .values(
                    meeting_id=meeting_id,
                    source="recall",
                    event_type=event_type,
                    event_timestamp=event_ts,
                    received_at=received_at,
                    payload_json=payload,
                    dedupe_key=key,
                    signature_valid=True,
                )
                .on_conflict_do_nothing(index_elements=["dedupe_key"])
                .returning(MeetingEvent.id)
            )
            inserted = s.execute(stmt).scalar_one_or_none()
            if inserted is None:
                outcome = "duplicate"
        else:
            outcome = "missing_bot_id"

        s.add(WebhookDelivery(
            meeting_id=meeting_id,
            event_type=event_type,
            headers_json=headers,
            signature_valid=True,
            remote_addr=remote_addr,
            response_code=200,
        ))
        s.commit()

    return {"status": outcome, "event_type": event_type}

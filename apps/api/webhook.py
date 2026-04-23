from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db import SessionLocal
from models import Meeting, MeetingEvent, WebhookDelivery
from streams import dispatch_event

log = logging.getLogger("webhook")
router = APIRouter(tags=["webhook"])

SVIX_TOLERANCE_SECONDS = 5 * 60


def _webhook_secret() -> str:
    return os.environ.get("RECALL_WEBHOOK_SECRET", "")


def _svix_secret_bytes(secret: str) -> Optional[bytes]:
    if not secret.startswith("whsec_"):
        return None
    try:
        return base64.b64decode(secret[len("whsec_"):])
    except Exception:
        return None


def verify_svix(
    body: bytes,
    msg_id: Optional[str],
    msg_timestamp: Optional[str],
    msg_signature: Optional[str],
    secret: str,
    *,
    tolerance_seconds: int = SVIX_TOLERANCE_SECONDS,
) -> bool:
    """Verify a Svix-style webhook signature.

    Headers: svix-id, svix-timestamp (unix seconds), svix-signature ("v1,<b64>" possibly space-separated).
    Signed payload: f"{msg_id}.{msg_timestamp}.{body}" with secret bytes from base64(secret[len('whsec_'):]).
    """
    if not (msg_id and msg_timestamp and msg_signature):
        return False
    key = _svix_secret_bytes(secret)
    if key is None:
        return False
    try:
        ts_int = int(msg_timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts_int) > tolerance_seconds:
        return False
    signed_content = f"{msg_id}.{msg_timestamp}.{body.decode('utf-8', errors='replace')}".encode()
    expected = base64.b64encode(hmac.new(key, signed_content, hashlib.sha256).digest()).decode()
    for part in msg_signature.split():
        if "," not in part:
            continue
        scheme, sig = part.split(",", 1)
        if scheme == "v1" and hmac.compare_digest(expected, sig):
            return True
    return False


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _dedupe_key(msg_id: Optional[str], bot_id: str, event_type: str, event_ts: str, payload: Any) -> str:
    """If svix-id is present use it as the authoritative key, else fall back to content hash."""
    if msg_id:
        return f"svix:{msg_id}"
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
        or (data.get("bot") if isinstance(data.get("bot"), str) else None)
        or payload.get("bot_id")
    )
    event_ts = _parse_ts(
        payload.get("timestamp") or data.get("timestamp") or (data.get("created_at"))
    )
    return bot_id, event_type, event_ts


@router.post("/webhook/recall")
async def ingest(request: Request):
    raw = await request.body()
    received_at = datetime.now(tz=timezone.utc)
    remote_addr = request.client.host if request.client else None
    # capture everything so failures are debuggable from the DB
    all_headers = {k.lower(): v for k, v in request.headers.items()}
    # Svix supports both "svix-*" (legacy) and "webhook-*" (newer, vendor-neutral).
    # Recall sends the "webhook-*" variant.
    svix_id = all_headers.get("webhook-id") or all_headers.get("svix-id")
    svix_ts = all_headers.get("webhook-timestamp") or all_headers.get("svix-timestamp")
    svix_sig = all_headers.get("webhook-signature") or all_headers.get("svix-signature")

    if not verify_svix(raw, svix_id, svix_ts, svix_sig, _webhook_secret()):
        with SessionLocal() as s:
            s.add(WebhookDelivery(
                event_type=None,
                headers_json=all_headers,
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
                headers_json=all_headers,
                signature_valid=True,
                remote_addr=remote_addr,
                response_code=400,
            ))
            s.commit()
        return JSONResponse({"error": "malformed json"}, status_code=400)

    bot_id, event_type, event_ts = _extract_envelope(payload)
    inserted: Optional[int] = None

    with SessionLocal() as s:
        meeting_id = None
        if bot_id:
            meeting_id = s.execute(
                select(Meeting.id).where(Meeting.recall_bot_id == bot_id)
            ).scalar_one_or_none()

        outcome = "accepted"
        if bot_id:
            key = _dedupe_key(svix_id, bot_id, event_type, event_ts.isoformat(), payload)
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
            headers_json=all_headers,
            signature_valid=True,
            remote_addr=remote_addr,
            response_code=200,
        ))
        s.commit()

    if outcome == "accepted" and inserted is not None and bot_id:
        await dispatch_event(bot_id, inserted, event_type, event_ts)

    return {"status": outcome, "event_type": event_type}

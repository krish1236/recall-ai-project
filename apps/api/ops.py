from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from db import SessionLocal
from models import (
    DeadLetterJob,
    Meeting,
    MeetingEvent,
    TranscriptUtterance,
    UtteranceSpan,
    WebhookDelivery,
)

log = logging.getLogger("ops")

router = APIRouter(tags=["ops"])


class EventDTO(BaseModel):
    id: int
    source: str
    event_type: str
    event_timestamp: datetime
    received_at: datetime
    persisted_at: datetime
    dedupe_key: str
    signature_valid: bool


class DeliveryDTO(BaseModel):
    id: int
    event_type: Optional[str]
    signature_valid: bool
    response_code: Optional[int]
    attempt_count: int
    received_at: datetime
    remote_addr: Optional[str]


class SpanDTO(BaseModel):
    utterance_id: UUID
    text: str
    speaker_label: Optional[str]
    start_ms: Optional[int]
    received_at: Optional[datetime]
    persisted_at: Optional[datetime]
    enqueued_at: Optional[datetime]
    classified_at: Optional[datetime]
    pushed_at: Optional[datetime]
    end_to_end_ms: Optional[int]


class DLQDTO(BaseModel):
    id: UUID
    job_kind: str
    error: Optional[str]
    status: str
    attempt_count: int
    created_at: datetime


class MetricsDTO(BaseModel):
    events_accepted: int
    webhook_deliveries_ok: int
    webhook_deliveries_bad_sig: int
    duplicates_absorbed: int
    utterance_count: int
    p50_end_to_end_ms: Optional[float]
    p95_end_to_end_ms: Optional[float]
    p99_end_to_end_ms: Optional[float]


class OpsResponse(BaseModel):
    meeting_id: UUID
    status: str
    events: list[EventDTO]
    deliveries: list[DeliveryDTO]
    utterance_spans: list[SpanDTO]
    dlq: list[DLQDTO]
    metrics: MetricsDTO


def _percentile(values: list[int], p: float) -> Optional[float]:
    if not values:
        return None
    sorted_v = sorted(values)
    k = int(len(sorted_v) * p / 100)
    k = min(k, len(sorted_v) - 1)
    return float(sorted_v[k])


@router.get("/meetings/{meeting_id}/ops", response_model=OpsResponse)
async def meeting_ops(meeting_id: UUID) -> OpsResponse:
    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        if meeting is None:
            raise HTTPException(404, "meeting not found")

        events = s.execute(
            select(MeetingEvent)
            .where(MeetingEvent.meeting_id == meeting_id)
            .order_by(MeetingEvent.event_timestamp, MeetingEvent.id)
        ).scalars().all()

        deliveries = s.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.meeting_id == meeting_id)
            .order_by(WebhookDelivery.received_at.desc())
            .limit(200)
        ).scalars().all()

        events_accepted = len(events)
        bad_sig = s.execute(
            select(func.count()).select_from(WebhookDelivery)
            .where(WebhookDelivery.meeting_id == meeting_id, WebhookDelivery.signature_valid.is_(False))
        ).scalar_one()
        deliveries_ok = s.execute(
            select(func.count()).select_from(WebhookDelivery)
            .where(WebhookDelivery.meeting_id == meeting_id, WebhookDelivery.signature_valid.is_(True))
        ).scalar_one()
        duplicates = max(0, deliveries_ok - events_accepted)

        utterances = s.execute(
            select(TranscriptUtterance)
            .where(TranscriptUtterance.meeting_id == meeting_id)
            .order_by(TranscriptUtterance.start_ms, TranscriptUtterance.created_at)
        ).scalars().all()
        utterance_ids = [u.id for u in utterances]
        span_rows = s.execute(
            select(UtteranceSpan).where(UtteranceSpan.utterance_id.in_(utterance_ids))
        ).scalars().all() if utterance_ids else []
        spans_by_id = {r.utterance_id: r for r in span_rows}

        # received_at for each utterance comes from its source_event row
        source_event_by_utt = {}
        event_by_id = {e.id: e for e in events}
        for u in utterances:
            if u.source_event_id and u.source_event_id in event_by_id:
                source_event_by_utt[u.id] = event_by_id[u.source_event_id]

        spans_dto: list[SpanDTO] = []
        end_to_end_values: list[int] = []
        for u in utterances:
            sp = spans_by_id.get(u.id)
            received = source_event_by_utt.get(u.id).received_at if source_event_by_utt.get(u.id) else None
            persisted = sp.persisted_at if sp else None
            enqueued = sp.enqueued_at if sp else None
            classified = sp.classified_at if sp else None
            pushed = sp.pushed_at if sp else None
            # end-to-end = received → pushed (first user-visible signal)
            end_to_end_ms: Optional[int] = None
            if received and pushed:
                end_to_end_ms = int((pushed - received).total_seconds() * 1000)
                end_to_end_values.append(end_to_end_ms)
            spans_dto.append(SpanDTO(
                utterance_id=u.id,
                text=u.text,
                speaker_label=u.speaker_label,
                start_ms=u.start_ms,
                received_at=received,
                persisted_at=persisted,
                enqueued_at=enqueued,
                classified_at=classified,
                pushed_at=pushed,
                end_to_end_ms=end_to_end_ms,
            ))

        dlq_rows = s.execute(
            select(DeadLetterJob)
            .where(DeadLetterJob.meeting_id == meeting_id)
            .order_by(DeadLetterJob.created_at.desc())
        ).scalars().all()

        return OpsResponse(
            meeting_id=meeting.id,
            status=meeting.status,
            events=[
                EventDTO(
                    id=e.id, source=e.source, event_type=e.event_type,
                    event_timestamp=e.event_timestamp, received_at=e.received_at,
                    persisted_at=e.persisted_at, dedupe_key=e.dedupe_key,
                    signature_valid=e.signature_valid,
                ) for e in events
            ],
            deliveries=[
                DeliveryDTO(
                    id=d.id, event_type=d.event_type, signature_valid=d.signature_valid,
                    response_code=d.response_code, attempt_count=d.attempt_count,
                    received_at=d.received_at, remote_addr=d.remote_addr,
                ) for d in deliveries
            ],
            utterance_spans=spans_dto,
            dlq=[
                DLQDTO(
                    id=j.id, job_kind=j.job_kind, error=j.error, status=j.status,
                    attempt_count=j.attempt_count, created_at=j.created_at,
                ) for j in dlq_rows
            ],
            metrics=MetricsDTO(
                events_accepted=events_accepted,
                webhook_deliveries_ok=deliveries_ok,
                webhook_deliveries_bad_sig=bad_sig,
                duplicates_absorbed=duplicates,
                utterance_count=len(utterances),
                p50_end_to_end_ms=_percentile(end_to_end_values, 50),
                p95_end_to_end_ms=_percentile(end_to_end_values, 95),
                p99_end_to_end_ms=_percentile(end_to_end_values, 99),
            ),
        )

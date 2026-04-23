from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, desc, select

from db import SessionLocal
from models import (
    ActionItem,
    DeadLetterJob,
    Insight,
    InsightEvidence,
    Meeting,
    Summary,
    TranscriptUtterance,
    UtteranceSpan,
)

log = logging.getLogger("admin")

router = APIRouter(tags=["admin"], prefix="/admin")


class DeadLetterDTO(BaseModel):
    id: UUID
    job_kind: str
    meeting_id: Optional[UUID]
    error: Optional[str]
    attempt_count: int
    status: str
    created_at: datetime
    last_attempt_at: datetime


class RetryResult(BaseModel):
    id: UUID
    status: str
    detail: Optional[str] = None


@router.get("/dlq", response_model=list[DeadLetterDTO])
async def list_dlq(
    status: Optional[str] = Query(None),
    meeting_id: Optional[UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> list[DeadLetterDTO]:
    with SessionLocal() as s:
        stmt = select(DeadLetterJob).order_by(desc(DeadLetterJob.created_at)).limit(limit)
        if status:
            stmt = stmt.where(DeadLetterJob.status == status)
        if meeting_id:
            stmt = stmt.where(DeadLetterJob.meeting_id == meeting_id)
        rows = s.execute(stmt).scalars().all()
        return [
            DeadLetterDTO(
                id=r.id, job_kind=r.job_kind, meeting_id=r.meeting_id,
                error=r.error, attempt_count=r.attempt_count, status=r.status,
                created_at=r.created_at, last_attempt_at=r.last_attempt_at,
            ) for r in rows
        ]


@router.post("/dlq/{job_id}/resolve", response_model=RetryResult)
async def resolve_dlq(job_id: UUID) -> RetryResult:
    with SessionLocal() as s:
        job = s.get(DeadLetterJob, job_id)
        if job is None:
            raise HTTPException(404, "dlq job not found")
        job.status = "resolved"
        job.last_attempt_at = datetime.now(tz=timezone.utc)
        s.commit()
        return RetryResult(id=job.id, status="resolved")


@router.post("/dlq/{job_id}/retry", response_model=RetryResult)
async def retry_dlq(job_id: UUID) -> RetryResult:
    with SessionLocal() as s:
        job = s.get(DeadLetterJob, job_id)
        if job is None:
            raise HTTPException(404, "dlq job not found")
        job_kind = job.job_kind
        meeting_id = job.meeting_id
        payload = dict(job.payload_json or {})

    if job_kind == "classify":
        return await _retry_classify(job_id, meeting_id, payload)
    if job_kind == "synthesize":
        return await _retry_synthesize(job_id, meeting_id)

    with SessionLocal() as s:
        job = s.get(DeadLetterJob, job_id)
        if job is not None:
            job.attempt_count += 1
            job.last_attempt_at = datetime.now(tz=timezone.utc)
            s.commit()
    return RetryResult(
        id=job_id, status="unsupported",
        detail=f"no automatic retry for job_kind={job_kind}",
    )


async def _retry_classify(job_id: UUID, meeting_id: Optional[UUID], payload: dict) -> RetryResult:
    from intelligence.batcher import Batcher
    from intelligence.classifier import AnthropicClient, SignalClassifier
    from models import TranscriptUtterance

    if meeting_id is None:
        return RetryResult(id=job_id, status="error", detail="no meeting_id")
    utt_ids_raw = payload.get("batch_utterance_ids") or []
    try:
        utt_ids = [UUID(u) for u in utt_ids_raw]
    except Exception:
        return RetryResult(id=job_id, status="error", detail="malformed payload")

    if not utt_ids:
        return RetryResult(id=job_id, status="error", detail="empty batch")

    classifier = SignalClassifier(client=AnthropicClient())
    batcher = Batcher(session_factory=SessionLocal, classifier=classifier)

    with SessionLocal() as s:
        batch = s.execute(
            select(TranscriptUtterance).where(TranscriptUtterance.id.in_(utt_ids))
        ).scalars().all()
    if not batch:
        _update_job(job_id, attempt_incr=1)
        return RetryResult(id=job_id, status="error", detail="utterances not found")

    try:
        await batcher._run_flush(meeting_id, [u.id for u in batch])
    except Exception as e:  # noqa: BLE001
        _update_job(job_id, attempt_incr=1, mark_failed=True)
        return RetryResult(id=job_id, status="error", detail=str(e)[:500])

    _update_job(job_id, attempt_incr=1, resolve=True)
    return RetryResult(id=job_id, status="resolved")


async def _retry_synthesize(job_id: UUID, meeting_id: Optional[UUID]) -> RetryResult:
    from intelligence.classifier import AnthropicClient
    from intelligence.synthesizer import Synthesizer

    if meeting_id is None:
        return RetryResult(id=job_id, status="error", detail="no meeting_id")
    synth = Synthesizer(client=AnthropicClient())
    try:
        with SessionLocal() as s:
            output, _ = await synth.synthesize_and_persist(s, meeting_id)
            s.commit()
    except Exception as e:  # noqa: BLE001
        _update_job(job_id, attempt_incr=1, mark_failed=True)
        return RetryResult(id=job_id, status="error", detail=str(e)[:500])

    if output is None:
        _update_job(job_id, attempt_incr=1, mark_failed=True)
        return RetryResult(id=job_id, status="error", detail="synthesis returned empty")

    _update_job(job_id, attempt_incr=1, resolve=True)
    return RetryResult(id=job_id, status="resolved")


DEMO_MEETING_ID = UUID("00000000-0000-0000-0000-000000000001")

_DEMO_UTTERANCES: list[tuple[str, str, int]] = [
    ("rep", "Hey thanks for hopping on the call.", 1000),
    ("customer", "Yeah happy to. We've been comparing a few tools.", 3500),
    ("rep", "What are you looking at?", 6500),
    ("customer", "Honestly mostly Gong and Fireflies. The price on Gong is about half yours right now.", 8000),
    ("rep", "Understood. Can you tell me more about what pricing tier you need?", 12500),
    ("customer", "We really need Salesforce sync and that's non-negotiable for our sales team.", 15500),
    ("rep", "Got it. And on timeline?", 19500),
    ("customer", "We need to decide by end of next week. Can you send over a proposal by Friday?", 21000),
]


@router.post("/seed-demo")
async def seed_demo() -> dict:
    """Idempotently create the canonical Acme-discovery demo meeting with
    real classifier + synthesizer output. Intended for one-click seeding of
    an empty prod database so the Inbox has something interesting to show."""
    from intelligence.classifier import AnthropicClient, SignalClassifier
    from intelligence.synthesizer import Synthesizer

    mid = DEMO_MEETING_ID
    now = datetime.now(tz=timezone.utc)
    started = now - timedelta(minutes=5)

    with SessionLocal() as s:
        existing = s.get(Meeting, mid)
        if existing is None:
            s.add(Meeting(
                id=mid,
                title="Acme Q2 discovery",
                meeting_url="https://meet.google.com/demo-acme",
                meeting_type="discovery",
                owner_name="rep@acme.com",
                status="in_call",
                recall_bot_id="bot_demo_seed",
                started_at=started,
            ))

        # wipe derived rows so re-seeding is clean
        insight_ids = select(Insight.id).where(Insight.meeting_id == mid).scalar_subquery()
        utt_ids = select(TranscriptUtterance.id).where(TranscriptUtterance.meeting_id == mid).scalar_subquery()
        s.execute(delete(InsightEvidence).where(InsightEvidence.insight_id.in_(insight_ids)))
        s.execute(delete(Insight).where(Insight.meeting_id == mid))
        s.execute(delete(ActionItem).where(ActionItem.meeting_id == mid))
        s.execute(delete(Summary).where(Summary.meeting_id == mid))
        s.execute(delete(UtteranceSpan).where(UtteranceSpan.utterance_id.in_(utt_ids)))
        s.execute(delete(TranscriptUtterance).where(TranscriptUtterance.meeting_id == mid))

        for speaker, text, start_ms in _DEMO_UTTERANCES:
            s.add(TranscriptUtterance(
                meeting_id=mid, text=text, speaker_label=speaker,
                is_partial=False, start_ms=start_ms, end_ms=start_ms + 2000,
            ))
        s.commit()

    client = AnthropicClient()
    classifier = SignalClassifier(client=client)
    with SessionLocal() as s:
        utts = s.execute(
            select(TranscriptUtterance)
            .where(TranscriptUtterance.meeting_id == mid)
            .order_by(TranscriptUtterance.start_ms)
        ).scalars().all()
        insights, _ = await classifier.classify_and_persist(s, mid, list(utts), [])
        s.commit()

    synth = Synthesizer(client=client)
    with SessionLocal() as s:
        output, _ = await synth.synthesize_and_persist(s, mid)
        m = s.get(Meeting, mid)
        m.status = "done"
        m.state_changed_at = now
        m.ended_at = now
        s.commit()

    return {
        "meeting_id": str(mid),
        "status": "seeded",
        "utterances": len(_DEMO_UTTERANCES),
        "insights": len(insights),
        "synthesis": output is not None,
    }


@router.post("/replay/{meeting_id}")
async def replay_meeting_route(meeting_id: UUID, bg: BackgroundTasks) -> dict:
    """Kick off a deterministic replay of the meeting's event log. Returns
    immediately; the UI should re-fetch the meeting detail + ops after a short
    delay (or react to subsequent pub/sub state frames)."""
    from replay import replay_meeting as _replay

    async def _runner() -> None:
        try:
            await _replay(meeting_id)
        except Exception:  # noqa: BLE001
            log.exception("replay failed for meeting %s", meeting_id)

    bg.add_task(_runner)
    return {"meeting_id": str(meeting_id), "status": "queued"}


def _update_job(
    job_id: UUID,
    *,
    attempt_incr: int = 0,
    resolve: bool = False,
    mark_failed: bool = False,
) -> None:
    with SessionLocal() as s:
        job = s.get(DeadLetterJob, job_id)
        if job is None:
            return
        if attempt_incr:
            job.attempt_count += attempt_incr
        job.last_attempt_at = datetime.now(tz=timezone.utc)
        if resolve:
            job.status = "resolved"
        elif mark_failed and job.status != "wont_fix":
            job.status = "open"
        s.commit()

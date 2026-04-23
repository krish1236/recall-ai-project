from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select

from db import SessionLocal
from models import DeadLetterJob

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

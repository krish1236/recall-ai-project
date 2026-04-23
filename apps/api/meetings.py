from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from db import SessionLocal
from models import ActionItem, DeadLetterJob, Insight, InsightEvidence, Meeting, MeetingEvent, Summary, TranscriptUtterance
from recall_client import DEFAULT_REALTIME_EVENTS, RecallClient, RecallError
from streams import publish_live

log = logging.getLogger("meetings")

router = APIRouter(tags=["meetings"])


class CreateMeetingRequest(BaseModel):
    meeting_url: str
    account_id: Optional[UUID] = None
    title: Optional[str] = None
    meeting_type: Optional[str] = None
    owner_name: Optional[str] = None


class CreateMeetingResponse(BaseModel):
    meeting_id: UUID
    recall_bot_id: Optional[str]
    status: str


class InsightDTO(BaseModel):
    id: UUID
    type: str
    title: str
    description: Optional[str]
    severity: Optional[str]
    confidence: Optional[float]
    created_at: datetime
    evidence_utterance_ids: list[UUID]


class UtteranceDTO(BaseModel):
    id: UUID
    speaker_label: Optional[str]
    text: str
    start_ms: Optional[int]
    end_ms: Optional[int]
    created_at: datetime


class ActionItemDTO(BaseModel):
    id: UUID
    owner_name: Optional[str]
    action_text: str
    due_hint: Optional[str]
    status: str


class SummaryDTO(BaseModel):
    id: UUID
    summary_type: str
    content_markdown: str


class MeetingListItem(BaseModel):
    id: UUID
    title: Optional[str]
    meeting_url: Optional[str]
    status: str
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    created_at: datetime
    top_insight_title: Optional[str]
    top_insight_type: Optional[str]
    insight_count: int
    has_high_severity: bool


class MeetingDetail(BaseModel):
    id: UUID
    title: Optional[str]
    meeting_url: Optional[str]
    status: str
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    recall_bot_id: Optional[str]
    owner_name: Optional[str]
    utterances: list[UtteranceDTO]
    insights: list[InsightDTO]
    action_items: list[ActionItemDTO]
    summaries: list[SummaryDTO]


def get_recall_client() -> RecallClient:
    return RecallClient.from_env()


def _webhook_public_url() -> str:
    return os.environ.get("WEBHOOK_PUBLIC_URL", "http://localhost:8000/webhook/recall")


@router.post("/meetings", response_model=CreateMeetingResponse, status_code=201)
async def create_meeting(
    req: CreateMeetingRequest,
    recall: RecallClient = Depends(get_recall_client),
) -> CreateMeetingResponse:
    with SessionLocal() as s:
        meeting = Meeting(
            meeting_url=req.meeting_url,
            account_id=req.account_id,
            title=req.title,
            meeting_type=req.meeting_type,
            owner_name=req.owner_name,
            status="requested",
        )
        s.add(meeting)
        s.commit()
        s.refresh(meeting)
        meeting_id = meeting.id

    try:
        bot = await recall.create_bot(
            meeting_url=req.meeting_url,
            webhook_url=_webhook_public_url(),
            webhook_events=DEFAULT_REALTIME_EVENTS,
            bot_name=req.title or "Recall Demo Bot",
        )
    except RecallError as e:
        log.warning("recall create_bot failed for meeting %s: %s", meeting_id, e)
        with SessionLocal() as s:
            m = s.get(Meeting, meeting_id)
            if m is not None:
                m.status = "failed"
                m.state_changed_at = datetime.now(tz=timezone.utc)
                s.commit()
        raise HTTPException(status_code=502, detail=f"recall api error: {e}")

    bot_id = bot.get("id")
    now = datetime.now(tz=timezone.utc)
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        if m is None:
            raise HTTPException(status_code=500, detail="meeting vanished mid-request")
        m.recall_bot_id = bot_id
        m.status = "joining"
        m.state_changed_at = now
        s.commit()

    return CreateMeetingResponse(
        meeting_id=meeting_id,
        recall_bot_id=bot_id,
        status="joining",
    )


@router.get("/meetings", response_model=list[MeetingListItem])
async def list_meetings(
    status: Optional[str] = Query(None, description="filter by exact status"),
    limit: int = Query(50, ge=1, le=200),
) -> list[MeetingListItem]:
    with SessionLocal() as s:
        stmt = select(Meeting).order_by(desc(Meeting.created_at)).limit(limit)
        if status:
            stmt = stmt.where(Meeting.status == status)
        meetings = s.execute(stmt).scalars().all()

        # one query per meeting for the top insight + count — good enough for 50 rows
        out: list[MeetingListItem] = []
        for m in meetings:
            top = s.execute(
                select(Insight)
                .where(Insight.meeting_id == m.id)
                .order_by(desc(Insight.confidence.is_(None)), desc(Insight.confidence))
                .limit(1)
            ).scalars().first()
            count = s.execute(
                select(func.count()).select_from(Insight).where(Insight.meeting_id == m.id)
            ).scalar_one()
            has_high = s.execute(
                select(func.count()).select_from(Insight)
                .where(Insight.meeting_id == m.id, Insight.severity == "high")
            ).scalar_one() > 0
            out.append(MeetingListItem(
                id=m.id, title=m.title, meeting_url=m.meeting_url, status=m.status,
                started_at=m.started_at, ended_at=m.ended_at, created_at=m.created_at,
                top_insight_title=top.title if top else None,
                top_insight_type=top.type if top else None,
                insight_count=count, has_high_severity=has_high,
            ))
        return out


@router.post("/meetings/{meeting_id}/finalize", status_code=202)
async def finalize_meeting(meeting_id: UUID, bg: BackgroundTasks) -> dict:
    """Simulate end-of-call: move to processing and kick off synthesis."""
    now = datetime.now(tz=timezone.utc)
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        if m is None:
            raise HTTPException(404, "meeting not found")
        if m.status in ("done", "failed"):
            return {"meeting_id": str(meeting_id), "status": m.status, "note": "already terminal"}
        m.status = "processing"
        m.state_changed_at = now
        if m.ended_at is None:
            m.ended_at = now
        s.commit()

    await publish_live(meeting_id, "state", {"status": "processing", "state_changed_at": now.isoformat()})
    bg.add_task(_run_synthesis, meeting_id)
    return {"meeting_id": str(meeting_id), "status": "processing"}


async def _run_synthesis(meeting_id: UUID) -> None:
    from intelligence.classifier import AnthropicClient
    from intelligence.synthesizer import Synthesizer

    log.info("synthesis started for meeting %s", meeting_id)
    try:
        client = AnthropicClient()
        synth = Synthesizer(client=client)
        with SessionLocal() as s:
            output, outcome = await synth.synthesize_and_persist(s, meeting_id)
            s.commit()
    except Exception as e:  # noqa: BLE001
        log.exception("synthesis failed for meeting %s: %s", meeting_id, e)
        with SessionLocal() as s:
            m = s.get(Meeting, meeting_id)
            if m is not None:
                m.status = "failed"
                m.state_changed_at = datetime.now(tz=timezone.utc)
                s.commit()
            s.add(DeadLetterJob(
                job_kind="synthesize",
                meeting_id=meeting_id,
                payload_json={"meeting_id": str(meeting_id)},
                error=str(e)[:1000],
                attempt_count=1,
                status="open",
            ))
            s.commit()
        await publish_live(meeting_id, "state", {"status": "failed"})
        return

    now = datetime.now(tz=timezone.utc)
    final_status = "done" if output is not None else "failed"
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        if m is None:
            return
        m.status = final_status
        m.state_changed_at = now
        s.commit()
    await publish_live(meeting_id, "state", {"status": final_status, "state_changed_at": now.isoformat()})
    await publish_live(meeting_id, "summary_ready", {})
    log.info("synthesis done for meeting %s outcome=%s", meeting_id, outcome)


@router.post("/meetings/{meeting_id}/crm-push", status_code=201)
async def crm_push(meeting_id: UUID) -> dict:
    """Mock CRM push — writes an internal meeting event so it's visible in the
    event log (and, later, Mission Control). Pretends to POST to a CRM."""
    now = datetime.now(tz=timezone.utc)
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        if m is None:
            raise HTTPException(404, "meeting not found")
        crm_note = s.execute(
            select(Summary).where(
                Summary.meeting_id == meeting_id,
                Summary.summary_type == "crm_note",
            )
        ).scalars().first()
        note_text = crm_note.content_markdown if crm_note else ""
        dedupe_key = f"crm-push:{meeting_id}:{now.isoformat()}"
        s.add(MeetingEvent(
            meeting_id=meeting_id,
            source="internal",
            event_type="internal.crm_pushed",
            event_timestamp=now,
            received_at=now,
            payload_json={
                "meeting_id": str(meeting_id),
                "note": note_text,
                "destination": "mock-crm",
            },
            dedupe_key=dedupe_key,
            signature_valid=True,
        ))
        s.commit()
    await publish_live(meeting_id, "crm_pushed", {"at": now.isoformat()})
    return {"meeting_id": str(meeting_id), "pushed_at": now.isoformat()}


@router.get("/meetings/{meeting_id}", response_model=MeetingDetail)
async def get_meeting(meeting_id: UUID) -> MeetingDetail:
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        if m is None:
            raise HTTPException(status_code=404, detail="meeting not found")

        utts = s.execute(
            select(TranscriptUtterance)
            .where(TranscriptUtterance.meeting_id == meeting_id)
            .order_by(TranscriptUtterance.start_ms, TranscriptUtterance.created_at)
        ).scalars().all()

        insights = s.execute(
            select(Insight)
            .where(Insight.meeting_id == meeting_id)
            .order_by(Insight.created_at)
        ).scalars().all()
        insight_ids = [i.id for i in insights]
        evidences = s.execute(
            select(InsightEvidence).where(InsightEvidence.insight_id.in_(insight_ids))
        ).scalars().all() if insight_ids else []
        ev_by_insight: dict[UUID, list[UUID]] = {i.id: [] for i in insights}
        for ev in evidences:
            ev_by_insight.setdefault(ev.insight_id, []).append(ev.utterance_id)

        actions = s.execute(
            select(ActionItem).where(ActionItem.meeting_id == meeting_id)
        ).scalars().all()

        summaries = s.execute(
            select(Summary).where(Summary.meeting_id == meeting_id)
        ).scalars().all()

        return MeetingDetail(
            id=m.id, title=m.title, meeting_url=m.meeting_url, status=m.status,
            started_at=m.started_at, ended_at=m.ended_at, recall_bot_id=m.recall_bot_id,
            owner_name=m.owner_name,
            utterances=[
                UtteranceDTO(
                    id=u.id, speaker_label=u.speaker_label, text=u.text,
                    start_ms=u.start_ms, end_ms=u.end_ms, created_at=u.created_at,
                ) for u in utts
            ],
            insights=[
                InsightDTO(
                    id=i.id, type=i.type, title=i.title, description=i.description,
                    severity=i.severity,
                    confidence=float(i.confidence) if i.confidence is not None else None,
                    created_at=i.created_at,
                    evidence_utterance_ids=ev_by_insight.get(i.id, []),
                ) for i in insights
            ],
            action_items=[
                ActionItemDTO(
                    id=a.id, owner_name=a.owner_name, action_text=a.action_text,
                    due_hint=a.due_hint, status=a.status,
                ) for a in actions
            ],
            summaries=[
                SummaryDTO(id=s.id, summary_type=s.summary_type, content_markdown=s.content_markdown)
                for s in summaries
            ],
        )

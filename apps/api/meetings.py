from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import SessionLocal
from models import Meeting
from recall_client import DEFAULT_REALTIME_EVENTS, RecallClient, RecallError

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


@router.get("/meetings/{meeting_id}", response_model=CreateMeetingResponse)
async def get_meeting(meeting_id: UUID) -> CreateMeetingResponse:
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        if m is None:
            raise HTTPException(status_code=404, detail="meeting not found")
        return CreateMeetingResponse(
            meeting_id=m.id,
            recall_bot_id=m.recall_bot_id,
            status=m.status,
        )

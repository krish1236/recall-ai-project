from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from db import SessionLocal
from handlers import build_handlers
from models import (
    ActionItem,
    Insight,
    InsightEvidence,
    Meeting,
    MeetingEvent,
    Summary,
    TranscriptUtterance,
    UtteranceSpan,
)

log = logging.getLogger("replay")


async def replay_meeting(meeting_id: uuid.UUID) -> dict:
    """Wipe derived state for a meeting, then re-dispatch every event through
    the normal handlers. LLM calls are served from the cache so replay is
    deterministic and cheap when the original run used the same prompts.

    Returns a small summary so the caller can show "replayed N events" in the UI.
    """
    with SessionLocal() as s:
        events = list(s.execute(
            select(MeetingEvent)
            .where(MeetingEvent.meeting_id == meeting_id)
            .order_by(MeetingEvent.event_timestamp, MeetingEvent.id)
        ).scalars().all())
        if not events:
            return {"status": "empty", "events": 0}
        _wipe_derived(s, meeting_id)
        _reset_meeting_state(s, meeting_id)
        s.commit()

    handlers = _build_handlers_for_replay()
    processed = 0
    for event in events:
        await _run_event(handlers, event.id)
        processed += 1

    batcher = handlers["__batcher__"]
    await batcher.flush_all()

    log.info("replayed %d events for meeting %s", processed, meeting_id)
    return {"status": "replayed", "events": processed}


def _wipe_derived(session: Session, meeting_id: uuid.UUID) -> None:
    insight_ids_subq = select(Insight.id).where(Insight.meeting_id == meeting_id).scalar_subquery()
    utt_ids_subq = select(TranscriptUtterance.id).where(TranscriptUtterance.meeting_id == meeting_id).scalar_subquery()

    session.execute(delete(InsightEvidence).where(InsightEvidence.insight_id.in_(insight_ids_subq)))
    session.execute(delete(Insight).where(Insight.meeting_id == meeting_id))
    session.execute(delete(ActionItem).where(ActionItem.meeting_id == meeting_id))
    session.execute(delete(Summary).where(Summary.meeting_id == meeting_id))
    session.execute(delete(UtteranceSpan).where(UtteranceSpan.utterance_id.in_(utt_ids_subq)))
    session.execute(delete(TranscriptUtterance).where(TranscriptUtterance.meeting_id == meeting_id))


def _reset_meeting_state(session: Session, meeting_id: uuid.UUID) -> None:
    meeting = session.get(Meeting, meeting_id)
    if meeting is None:
        return
    meeting.status = "requested"
    meeting.state_changed_at = meeting.created_at
    meeting.started_at = None
    meeting.ended_at = None


def _build_handlers_for_replay() -> dict:
    from intelligence.batcher import Batcher
    from intelligence.classifier import AnthropicClient, SignalClassifier

    classifier = SignalClassifier(client=AnthropicClient())
    batcher = Batcher(session_factory=SessionLocal, classifier=classifier)
    handlers = dict(build_handlers(batcher=batcher))
    handlers["__batcher__"] = batcher
    return handlers


def _route(handlers: dict, event_type: str):
    if event_type in handlers:
        return handlers[event_type]
    for prefix, h in handlers.items():
        if prefix in ("__default__", "__batcher__"):
            continue
        if event_type.startswith(prefix + "."):
            return h
    return handlers.get("__default__")


async def _run_event(handlers: dict, event_id: int) -> None:
    with SessionLocal() as s:
        event = s.get(MeetingEvent, event_id)
        if event is None:
            return
        handler = _route(handlers, event.event_type)
        if handler is None:
            return
        try:
            await handler(event, s)
            s.commit()
        except Exception:  # noqa: BLE001
            s.rollback()
            log.exception("replay handler failed for event %s", event_id)

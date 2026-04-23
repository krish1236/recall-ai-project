from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from models import Meeting, MeetingEvent, TranscriptUtterance

log = logging.getLogger("handlers")

ALLOWED_TRANSITIONS: dict[Optional[str], set[str]] = {
    None: {"requested", "joining", "in_call", "processing", "done", "failed"},
    "requested": {"joining", "in_call", "failed"},
    "joining": {"in_call", "processing", "done", "failed"},
    "in_call": {"processing", "done", "failed"},
    "processing": {"done", "failed"},
    "done": set(),
    "failed": set(),
}

# Map Recall's bot status codes (from docs) to our internal state machine.
_STATUS_MAP = {
    "bot_requested": "requested",
    "bot_queued": "requested",
    "ready_to_join": "joining",
    "joining_call": "joining",
    "in_waiting_room": "joining",
    "in_call_not_recording": "in_call",
    "in_call_recording": "in_call",
    "recording": "in_call",
    "recording_done": "processing",
    "call_ended": "processing",
    "analyzing": "processing",
    "done": "done",
    "fatal": "failed",
    "timeout": "failed",
    "media_expired": "failed",
    # passthrough for tests using our own state names
    "requested": "requested",
    "joining": "joining",
    "in_call": "in_call",
    "processing": "processing",
    "failed": "failed",
}


def normalize_status(code: str) -> Optional[str]:
    return _STATUS_MAP.get(code)


def _extract_status_code(payload: dict) -> Optional[str]:
    data = payload.get("data") or {}
    status = data.get("status")
    if isinstance(status, dict):
        return status.get("code")
    if isinstance(status, str):
        return status
    return None


async def handle_status_change(event: MeetingEvent, session: Session) -> None:
    """Lifecycle state machine. HP-3 (status is a projection of events) and
    HP-4 (monotone timestamp; never regress state from a stale event)."""
    if event.meeting_id is None:
        log.debug("status_change with no meeting_id; skipping")
        return

    raw = _extract_status_code(event.payload_json)
    if not raw:
        log.warning("status_change without code: %s", event.payload_json)
        return
    target = normalize_status(raw)
    if target is None:
        log.warning("unknown recall status code=%r; ignoring", raw)
        return

    meeting = session.get(Meeting, event.meeting_id)
    if meeting is None:
        log.info("status_change for unknown meeting %s", event.meeting_id)
        return

    # HP-4 — event-time monotone: if event is older than current state change, drop it
    if meeting.state_changed_at is not None and event.event_timestamp < meeting.state_changed_at:
        log.info(
            "stale status_change rejected: meeting=%s current=%s incoming=%s (%s < %s)",
            meeting.id, meeting.status, target,
            event.event_timestamp.isoformat(), meeting.state_changed_at.isoformat(),
        )
        return

    if target == meeting.status:
        return  # idempotent no-op

    allowed = ALLOWED_TRANSITIONS.get(meeting.status, set())
    if target not in allowed:
        log.warning(
            "illegal transition meeting=%s %s -> %s; ignoring",
            meeting.id, meeting.status, target,
        )
        return

    log.info("meeting=%s transition %s -> %s", meeting.id, meeting.status, target)
    meeting.status = target
    meeting.state_changed_at = event.event_timestamp
    if target == "in_call" and meeting.started_at is None:
        meeting.started_at = event.event_timestamp
    elif target in ("done", "failed") and meeting.ended_at is None:
        meeting.ended_at = event.event_timestamp


async def handle_transcript_data(event: MeetingEvent, session: Session) -> None:
    """Project a finalized transcript event into the transcript_utterances table.

    Recall sends one event per finalized utterance; a single utterance contains
    many word timings grouped by speaker. We flatten to a single row.
    """
    if event.meeting_id is None:
        log.debug("transcript.data with no meeting_id; dropping")
        return
    data = event.payload_json.get("data") or {}
    words = data.get("words") or []
    if not words:
        return

    speaker = None
    participant = data.get("participant")
    if isinstance(participant, dict):
        speaker = participant.get("name") or participant.get("id")
    if speaker is None:
        speaker = data.get("speaker") or words[0].get("speaker")

    text_parts = [w.get("text", "") for w in words if w.get("text")]
    text = " ".join(text_parts).strip()
    if not text:
        return

    start = words[0].get("start") or 0.0
    end = words[-1].get("end") or start
    start_ms = int(float(start) * 1000)
    end_ms = int(float(end) * 1000)

    session.add(TranscriptUtterance(
        meeting_id=event.meeting_id,
        source_event_id=event.id,
        speaker_label=str(speaker) if speaker is not None else None,
        text=text,
        is_partial=False,
        start_ms=start_ms,
        end_ms=end_ms,
    ))


async def handle_transcript_partial(event: MeetingEvent, session: Session) -> None:
    """Partial data drives the UI preview only; we don't persist it."""
    return


async def handle_unknown(event: MeetingEvent, session: Session) -> None:
    log.debug("unhandled event_type=%s", event.event_type)


DEFAULT_HANDLERS = {
    "transcript.data": handle_transcript_data,
    "transcript.partial_data": handle_transcript_partial,
    "bot.status_change": handle_status_change,
    "__default__": handle_unknown,
}

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select
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


def _word_time(word: dict, key: str) -> float:
    """Handle both flat (`start`/`end`) and nested (`start_timestamp.relative`) shapes."""
    if key in word and word[key] is not None:
        return float(word[key])
    ts = word.get(f"{key}_timestamp")
    if isinstance(ts, dict):
        if "relative" in ts:
            return float(ts["relative"])
    return 0.0


def _extract_bot_id(payload: dict) -> Optional[str]:
    outer = payload.get("data") or {}
    bot = outer.get("bot")
    if isinstance(bot, dict) and bot.get("id"):
        return bot["id"]
    return outer.get("bot_id") or payload.get("bot_id")


async def _resolve_meeting_id(event: MeetingEvent, session: Session) -> Optional[Any]:
    """Late-bind to a meeting row when the webhook arrived before the meeting
    record existed (or after a truncate) — idempotent and cheap."""
    if event.meeting_id is not None:
        return event.meeting_id
    bot_id = _extract_bot_id(event.payload_json)
    if not bot_id:
        return None
    mid = session.execute(select(Meeting.id).where(Meeting.recall_bot_id == bot_id)).scalar_one_or_none()
    if mid is not None:
        event.meeting_id = mid  # persist the link for replay/debugging
    return mid


async def handle_transcript_data(event: MeetingEvent, session: Session) -> None:
    """Project a finalized transcript event into transcript_utterances.

    Recall's real payload nests utterance content at `payload.data.data` and uses
    `start_timestamp.relative` / `end_timestamp.relative` (seconds from recording
    start). We also accept the flatter shape some fixtures/tests use.
    """
    meeting_id = await _resolve_meeting_id(event, session)
    if meeting_id is None:
        log.debug("transcript.data with no resolvable meeting; dropping")
        return

    outer = event.payload_json.get("data") or {}
    inner = outer.get("data") if isinstance(outer.get("data"), dict) else None
    body = inner if inner is not None else outer
    words = body.get("words") or []
    if not words:
        return

    speaker = None
    participant = body.get("participant")
    if isinstance(participant, dict):
        speaker = participant.get("name") or participant.get("id")
    if speaker is None:
        speaker = body.get("speaker") or words[0].get("speaker")

    text_parts = [w.get("text", "") for w in words if w.get("text")]
    text = " ".join(text_parts).strip()
    if not text:
        return

    start_s = _word_time(words[0], "start")
    end_s = _word_time(words[-1], "end") or start_s
    start_ms = int(start_s * 1000)
    end_ms = int(end_s * 1000)

    session.add(TranscriptUtterance(
        meeting_id=meeting_id,
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

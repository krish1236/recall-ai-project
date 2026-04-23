from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from intelligence.cache import compute_cache_key, get_or_call, pricing_usd
from intelligence.classifier import AnthropicLike
from models import ActionItem, Insight, Meeting, Summary, TranscriptUtterance

log = logging.getLogger("synthesizer")

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 2048

TOOL_NAME = "record_synthesis"

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": "Record post-call synthesis for a customer meeting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "exec_summary": {
                "type": "string",
                "description": "Concise 3-5 sentence summary of the call for a sales manager. Plain markdown.",
            },
            "risk_level": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "Overall account/deal risk after this call.",
            },
            "action_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action_text": {"type": "string"},
                        "owner_name": {"type": "string"},
                        "due_hint": {"type": "string"},
                    },
                    "required": ["action_text"],
                },
            },
            "followup_email": {
                "type": "string",
                "description": "Full follow-up email body in markdown. Include greeting, 3-5 bullets, and a clear next step.",
            },
            "crm_note": {
                "type": "string",
                "description": "Compact CRM-friendly note, <= 500 chars, plain text.",
            },
        },
        "required": ["exec_summary", "action_items", "followup_email", "crm_note"],
    },
}

SYSTEM_PROMPT = """You are a senior sales or customer-success operator summarizing a customer call.

Your output will be read by a busy rep and stored in a CRM. Be accurate, concise, and concrete.

Rules:
- Never invent facts. If the transcript doesn't support a claim, don't make it.
- The action items must be specific and actionable with an owner and a due hint when the transcript allows.
- The follow-up email should sound like a real rep wrote it — not a template.
- The CRM note is one compact paragraph of the most valuable facts.
- Keep the exec summary to 3-5 sentences.

Always call the record_synthesis tool."""


def _render_transcript(utterances: list[TranscriptUtterance]) -> str:
    lines = []
    for u in utterances:
        speaker = u.speaker_label or "unknown"
        text = (u.text or "").strip().replace("\n", " ")
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines) if lines else "(no transcript)"


def _render_insights(insights: list[Insight]) -> str:
    if not insights:
        return "(no signals)"
    lines = []
    for i in insights:
        parts = [f"- [{i.type}] {i.title}"]
        if i.severity:
            parts.append(f"severity={i.severity}")
        if i.confidence is not None:
            parts.append(f"confidence={float(i.confidence):.2f}")
        if i.description:
            parts.append(f"— {i.description}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _build_user_prompt(meeting: Meeting, utterances: list[TranscriptUtterance], insights: list[Insight]) -> str:
    header_parts = []
    if meeting.title:
        header_parts.append(f"title: {meeting.title}")
    if meeting.meeting_type:
        header_parts.append(f"type: {meeting.meeting_type}")
    if meeting.owner_name:
        header_parts.append(f"owner: {meeting.owner_name}")
    header = " · ".join(header_parts) if header_parts else "(no metadata)"

    return (
        f"<meeting>\n{header}\n</meeting>\n\n"
        f"<signals>\n{_render_insights(insights)}\n</signals>\n\n"
        f"<transcript>\n{_render_transcript(utterances)}\n</transcript>"
    )


class Synthesizer:
    def __init__(
        self,
        client: AnthropicLike,
        model: str = DEFAULT_MODEL,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        self.client = client
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def synthesize_and_persist(
        self,
        session: Session,
        meeting_id: uuid.UUID,
    ) -> tuple[Optional[dict], str]:
        """Run synthesis for a meeting. Returns (parsed_output, cache_outcome)."""
        meeting = session.get(Meeting, meeting_id)
        if meeting is None:
            return None, "no_meeting"

        utterances = session.execute(
            select(TranscriptUtterance)
            .where(TranscriptUtterance.meeting_id == meeting_id)
            .order_by(TranscriptUtterance.start_ms, TranscriptUtterance.created_at)
        ).scalars().all()
        if not utterances:
            return None, "no_transcript"

        insights = session.execute(
            select(Insight).where(Insight.meeting_id == meeting_id).order_by(Insight.created_at)
        ).scalars().all()

        user_prompt = _build_user_prompt(meeting, list(utterances), list(insights))
        cache_key = compute_cache_key(
            self.model,
            [SYSTEM_PROMPT, user_prompt],
            self.temperature,
            TOOL_SCHEMA,
        )

        async def call_llm() -> dict:
            result = await self.client.create_message(
                model=self.model,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                tools=[TOOL_SCHEMA],
                tool_choice={"type": "tool", "name": TOOL_NAME},
                max_tokens=self.max_tokens,
                temperature=self.temperature,
            )
            t_in = int(result.get("token_in", 0))
            t_out = int(result.get("token_out", 0))
            return {
                "response": {"tool_input": result.get("tool_input", {})},
                "token_in": t_in,
                "token_out": t_out,
                "cost_usd": pricing_usd(self.model, t_in, t_out),
            }

        response, outcome = await get_or_call(session, cache_key, call_llm)
        tool_input = (response.get("tool_input") or {}) if isinstance(response, dict) else {}
        if not tool_input:
            log.warning("synthesis returned empty tool_input for meeting %s", meeting_id)
            return None, outcome

        _persist(session, meeting_id, tool_input)
        session.flush()
        return tool_input, outcome


def _persist(session: Session, meeting_id: uuid.UUID, output: dict) -> None:
    """Replace prior synthesis rows for a meeting, then write fresh ones."""
    session.execute(delete(Summary).where(Summary.meeting_id == meeting_id))
    session.execute(delete(ActionItem).where(ActionItem.meeting_id == meeting_id))

    exec_summary = output.get("exec_summary")
    if exec_summary:
        session.add(Summary(
            meeting_id=meeting_id,
            summary_type="executive_summary",
            content_markdown=exec_summary,
        ))

    followup = output.get("followup_email")
    if followup:
        session.add(Summary(
            meeting_id=meeting_id,
            summary_type="followup_email",
            content_markdown=followup,
        ))

    crm = output.get("crm_note")
    if crm:
        session.add(Summary(
            meeting_id=meeting_id,
            summary_type="crm_note",
            content_markdown=crm,
        ))

    for ai in output.get("action_items") or []:
        text = (ai.get("action_text") or "").strip()
        if not text:
            continue
        session.add(ActionItem(
            meeting_id=meeting_id,
            action_text=text[:500],
            owner_name=(ai.get("owner_name") or None),
            due_hint=(ai.get("due_hint") or None),
            status="open",
        ))

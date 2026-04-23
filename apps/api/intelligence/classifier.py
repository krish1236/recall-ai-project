from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Optional, Protocol

from sqlalchemy.orm import Session

from intelligence.cache import compute_cache_key, get_or_call, pricing_usd
from models import Insight, InsightEvidence, TranscriptUtterance

log = logging.getLogger("classifier")

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 1024

TOOL_NAME = "record_signals"

SIGNAL_TYPES = [
    "objection",
    "feature_request",
    "competitor_mention",
    "risk",
    "commitment",
    "customer_goal",
    "urgency",
]

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": "Record structured business signals found in the transcript batch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "signals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": SIGNAL_TYPES},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "source_aliases": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Reference labels (e.g. U4) from <batch> or <context> that evidence this signal.",
                        },
                    },
                    "required": ["type", "title", "source_aliases"],
                },
            }
        },
        "required": ["signals"],
    },
}

SYSTEM_PROMPT = """You analyze live customer-call transcripts for a sales or customer-success team.
From the utterances provided, extract clear, well-evidenced business signals.

Rules:
- Never invent. If nothing is clearly present, return an empty signals list.
- Every signal must cite at least one source_alias from <batch> or <context>.
- Titles are short (<=8 words). Descriptions are one sentence.
- Prefer confidence 0.6+ only when the signal is unambiguous.
- Prefer signals derived from the <batch>; use <context> only to disambiguate.

Always call the record_signals tool."""


class AnthropicLike(Protocol):
    """Minimal interface so tests can inject a fake."""

    async def create_message(
        self, *, model: str, system: str, messages: list[dict],
        tools: list[dict], tool_choice: dict, max_tokens: int, temperature: float,
    ) -> dict:
        ...


class AnthropicClient:
    """Thin wrapper around the Anthropic async SDK. Returns a plain dict."""

    def __init__(self, api_key: Optional[str] = None):
        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", ""))

    async def create_message(
        self, *, model, system, messages, tools, tool_choice, max_tokens, temperature,
    ):
        resp = await self._client.messages.create(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        tool_input: dict[str, Any] = {}
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                tool_input = dict(block.input)
                break
        return {
            "tool_input": tool_input,
            "token_in": resp.usage.input_tokens,
            "token_out": resp.usage.output_tokens,
        }


def _alias_map(utterances: list[TranscriptUtterance], prefix: str, start: int = 1) -> dict[str, TranscriptUtterance]:
    """Assign short aliases (C1, B1, ...) so the model doesn't have to reason over UUIDs."""
    return {f"{prefix}{start + i}": u for i, u in enumerate(utterances)}


def _render_block(aliases: dict[str, TranscriptUtterance]) -> str:
    lines = []
    for alias, u in aliases.items():
        speaker = u.speaker_label or "unknown"
        text = (u.text or "").strip().replace("\n", " ")
        lines.append(f"[{alias}] {speaker}: {text}")
    return "\n".join(lines) if lines else "(none)"


def _build_prompt(context: list[TranscriptUtterance], batch: list[TranscriptUtterance]) -> tuple[str, dict[str, TranscriptUtterance]]:
    ctx_map = _alias_map(context, "C")
    bat_map = _alias_map(batch, "B")
    user = (
        "<context>\n" + _render_block(ctx_map) + "\n</context>\n\n"
        "<batch>\n" + _render_block(bat_map) + "\n</batch>"
    )
    combined: dict[str, TranscriptUtterance] = {}
    combined.update(ctx_map)
    combined.update(bat_map)
    return user, combined


class SignalClassifier:
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

    async def classify_and_persist(
        self,
        session: Session,
        meeting_id: uuid.UUID,
        batch: list[TranscriptUtterance],
        context: list[TranscriptUtterance],
    ) -> tuple[list[Insight], str]:
        """Returns (persisted_insights, cache_outcome)."""
        if not batch:
            return [], "no_batch"

        user_prompt, alias_to_utt = _build_prompt(context, batch)
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
        signals = (response.get("tool_input") or {}).get("signals") or []
        persisted = _persist_signals(session, meeting_id, signals, alias_to_utt)
        session.flush()
        return persisted, outcome


def _persist_signals(
    session: Session,
    meeting_id: uuid.UUID,
    signals: list[dict],
    alias_to_utt: dict[str, TranscriptUtterance],
) -> list[Insight]:
    persisted: list[Insight] = []
    for sig in signals:
        if sig.get("type") not in SIGNAL_TYPES:
            log.warning("skipping signal with unknown type: %s", sig.get("type"))
            continue
        aliases = sig.get("source_aliases") or []
        evidence_utterances = [alias_to_utt[a] for a in aliases if a in alias_to_utt]
        if not evidence_utterances:
            log.info("skipping signal %r with no resolvable evidence aliases", sig.get("title"))
            continue
        insight = Insight(
            meeting_id=meeting_id,
            type=sig["type"],
            title=(sig.get("title") or "")[:200],
            description=sig.get("description"),
            severity=sig.get("severity"),
            confidence=sig.get("confidence"),
        )
        session.add(insight)
        session.flush()
        for u in evidence_utterances:
            session.add(InsightEvidence(
                insight_id=insight.id,
                utterance_id=u.id,
                evidence_text=(u.text or "")[:500],
            ))
        persisted.append(insight)
    return persisted

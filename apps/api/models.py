from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    external_crm_id: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    meetings: Mapped[list["Meeting"]] = relationship(back_populates="account")


class Meeting(Base):
    __tablename__ = "meetings"
    __table_args__ = (
        CheckConstraint(
            "status in ('requested','joining','in_call','processing','done','failed')",
            name="ck_meetings_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    title: Mapped[Optional[str]] = mapped_column(Text)
    meeting_url: Mapped[Optional[str]] = mapped_column(Text)
    meeting_type: Mapped[Optional[str]] = mapped_column(Text)
    recall_bot_id: Mapped[Optional[str]] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="requested")
    state_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    owner_name: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    account: Mapped[Optional[Account]] = relationship(back_populates="meetings")


class MeetingEvent(Base):
    __tablename__ = "meeting_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_meeting_events_dedupe_key"),
        Index("ix_meeting_events_meeting_id_event_timestamp", "meeting_id", "event_timestamp"),
        Index("ix_meeting_events_event_type", "event_type"),
        CheckConstraint("source in ('recall','internal')", name="ck_meeting_events_source"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    persisted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_deliveries_received_at", "received_at", postgresql_ops={"received_at": "DESC"}),
        Index("ix_webhook_deliveries_meeting_id", "meeting_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="SET NULL")
    )
    event_type: Mapped[Optional[str]] = mapped_column(Text)
    headers_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    remote_addr: Mapped[Optional[str]] = mapped_column(INET)
    response_code: Mapped[Optional[int]] = mapped_column(Integer)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TranscriptUtterance(Base):
    __tablename__ = "transcript_utterances"
    __table_args__ = (
        Index("ix_utterances_meeting_start", "meeting_id", "start_ms"),
        Index(
            "ix_utterances_text_tsv",
            "text_tsv",
            postgresql_using="gin",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    source_event_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("meeting_events.id", ondelete="SET NULL")
    )
    speaker_label: Mapped[Optional[str]] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    start_ms: Mapped[Optional[int]] = mapped_column(Integer)
    end_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    text_tsv: Mapped[Optional[Any]] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(text, ''))", persisted=True),
    )


class Insight(Base):
    __tablename__ = "insights"
    __table_args__ = (
        Index("ix_insights_meeting", "meeting_id"),
        CheckConstraint(
            "type in ('objection','feature_request','competitor_mention','risk','commitment','customer_goal','urgency')",
            name="ck_insights_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    severity: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(3, 2))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InsightEvidence(Base):
    __tablename__ = "insight_evidence"
    __table_args__ = (Index("ix_insight_evidence_insight", "insight_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    insight_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insights.id", ondelete="CASCADE"), nullable=False
    )
    utterance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transcript_utterances.id", ondelete="CASCADE"), nullable=False
    )
    evidence_text: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ActionItem(Base):
    __tablename__ = "action_items"
    __table_args__ = (
        Index("ix_action_items_meeting", "meeting_id"),
        CheckConstraint(
            "status in ('open','done','dropped')",
            name="ck_action_items_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    owner_name: Mapped[Optional[str]] = mapped_column(Text)
    action_text: Mapped[str] = mapped_column(Text, nullable=False)
    due_hint: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Summary(Base):
    __tablename__ = "summaries"
    __table_args__ = (
        Index("ix_summaries_meeting_type", "meeting_id", "summary_type"),
        Index(
            "ix_summaries_content_tsv",
            "content_tsv",
            postgresql_using="gin",
        ),
        CheckConstraint(
            "summary_type in ('executive_summary','followup_email','crm_note')",
            name="ck_summaries_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    summary_type: Mapped[str] = mapped_column(Text, nullable=False)
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    content_tsv: Mapped[Optional[Any]] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(content_markdown, ''))", persisted=True),
    )


class DeadLetterJob(Base):
    __tablename__ = "dead_letter_jobs"
    __table_args__ = (
        Index("ix_dlq_status", "status"),
        CheckConstraint(
            "status in ('open','resolved','wont_fix','circuit_open')",
            name="ck_dlq_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    job_kind: Mapped[str] = mapped_column(Text, nullable=False)
    meeting_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="SET NULL")
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LLMCache(Base):
    __tablename__ = "llm_cache"

    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    token_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UtteranceSpan(Base):
    __tablename__ = "utterance_spans"

    utterance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transcript_utterances.id", ondelete="CASCADE"), primary_key=True
    )
    recall_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    persisted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    enqueued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    classified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    pushed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

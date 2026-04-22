from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select

from models import (
    Account,
    ActionItem,
    DeadLetterJob,
    Insight,
    InsightEvidence,
    LLMCache,
    Meeting,
    MeetingEvent,
    Summary,
    TranscriptUtterance,
    UtteranceSpan,
    WebhookDelivery,
)


def _now():
    return datetime.now(tz=timezone.utc)


def test_round_trip_every_table(db):
    account = Account(name="Acme Corp", external_crm_id="acme-001")
    db.add(account)
    db.flush()

    meeting = Meeting(
        account_id=account.id,
        title="Discovery call",
        meeting_url="https://meet.google.com/abc-defg-hij",
        meeting_type="discovery",
        recall_bot_id="bot_test_001",
        status="requested",
        owner_name="rep@acme.com",
    )
    db.add(meeting)
    db.flush()

    dedupe = hashlib.sha256(b"bot_test_001|bot.status_change|in_call|t=1").hexdigest()
    event = MeetingEvent(
        meeting_id=meeting.id,
        source="recall",
        event_type="bot.status_change.in_call",
        event_timestamp=_now(),
        received_at=_now(),
        payload_json={"bot_id": "bot_test_001", "status": "in_call"},
        dedupe_key=dedupe,
        signature_valid=True,
    )
    db.add(event)
    db.flush()

    delivery = WebhookDelivery(
        meeting_id=meeting.id,
        event_type="bot.status_change.in_call",
        headers_json={"x-recall-signature": "…"},
        signature_valid=True,
        remote_addr="127.0.0.1",
        response_code=200,
    )
    db.add(delivery)

    utterance = TranscriptUtterance(
        meeting_id=meeting.id,
        source_event_id=event.id,
        speaker_label="customer",
        text="We need Salesforce sync by Friday.",
        is_partial=False,
        start_ms=1000,
        end_ms=4000,
    )
    db.add(utterance)
    db.flush()

    insight = Insight(
        meeting_id=meeting.id,
        type="feature_request",
        title="Salesforce sync",
        description="Customer asked for Salesforce integration.",
        severity="medium",
        confidence=0.88,
    )
    db.add(insight)
    db.flush()

    evidence = InsightEvidence(
        insight_id=insight.id,
        utterance_id=utterance.id,
        evidence_text="We need Salesforce sync by Friday.",
    )
    db.add(evidence)

    action = ActionItem(
        meeting_id=meeting.id,
        owner_name="rep@acme.com",
        action_text="Send proposal covering Salesforce integration.",
        due_hint="Friday",
        status="open",
    )
    db.add(action)

    summary = Summary(
        meeting_id=meeting.id,
        summary_type="executive_summary",
        content_markdown="# Acme discovery\nCustomer wants Salesforce sync.",
    )
    db.add(summary)

    dlq = DeadLetterJob(
        job_kind="classify",
        meeting_id=meeting.id,
        payload_json={"batch": ["..."]},
        error="anthropic timeout",
        attempt_count=3,
        status="open",
    )
    db.add(dlq)

    cache = LLMCache(
        cache_key="sha256:testkey",
        response_json={"insights": []},
        token_in=120,
        token_out=40,
        cost_usd=0.0004,
    )
    db.add(cache)

    span = UtteranceSpan(
        utterance_id=utterance.id,
        received_at=_now(),
        persisted_at=_now(),
    )
    db.add(span)

    db.commit()

    assert db.execute(select(Account).where(Account.id == account.id)).scalar_one().name == "Acme Corp"
    assert db.execute(select(Meeting).where(Meeting.id == meeting.id)).scalar_one().recall_bot_id == "bot_test_001"
    assert db.execute(select(MeetingEvent).where(MeetingEvent.id == event.id)).scalar_one().dedupe_key == dedupe
    assert db.execute(select(WebhookDelivery).where(WebhookDelivery.id == delivery.id)).scalar_one().signature_valid is True
    assert db.execute(select(TranscriptUtterance).where(TranscriptUtterance.id == utterance.id)).scalar_one().text.startswith("We need")
    assert db.execute(select(Insight).where(Insight.id == insight.id)).scalar_one().type == "feature_request"
    assert db.execute(select(InsightEvidence).where(InsightEvidence.id == evidence.id)).scalar_one().insight_id == insight.id
    assert db.execute(select(ActionItem).where(ActionItem.id == action.id)).scalar_one().status == "open"
    assert db.execute(select(Summary).where(Summary.id == summary.id)).scalar_one().summary_type == "executive_summary"
    assert db.execute(select(DeadLetterJob).where(DeadLetterJob.id == dlq.id)).scalar_one().status == "open"
    assert db.execute(select(LLMCache).where(LLMCache.cache_key == cache.cache_key)).scalar_one().token_in == 120
    assert db.execute(select(UtteranceSpan).where(UtteranceSpan.utterance_id == utterance.id)).scalar_one() is not None

    db.delete(account)
    db.commit()


def test_dedupe_key_unique(db):
    meeting = Meeting(
        recall_bot_id="bot_dedupe_test",
        status="requested",
    )
    db.add(meeting)
    db.flush()

    dedupe = hashlib.sha256(b"same-key").hexdigest()
    e1 = MeetingEvent(
        meeting_id=meeting.id,
        source="recall",
        event_type="transcript.data",
        event_timestamp=_now(),
        received_at=_now(),
        payload_json={"n": 1},
        dedupe_key=dedupe,
        signature_valid=True,
    )
    db.add(e1)
    db.commit()

    e2 = MeetingEvent(
        meeting_id=meeting.id,
        source="recall",
        event_type="transcript.data",
        event_timestamp=_now(),
        received_at=_now(),
        payload_json={"n": 2},
        dedupe_key=dedupe,
        signature_valid=True,
    )
    db.add(e2)
    import sqlalchemy.exc
    import pytest
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        db.commit()
    db.rollback()

    db.delete(meeting)
    db.commit()


def test_tsvector_search(db):
    meeting = Meeting(
        recall_bot_id="bot_tsv_test",
        status="in_call",
    )
    db.add(meeting)
    db.flush()

    u = TranscriptUtterance(
        meeting_id=meeting.id,
        text="The pricing is too expensive for our budget.",
        is_partial=False,
    )
    db.add(u)
    db.commit()

    from sqlalchemy import text as sql_text
    row = db.execute(
        sql_text(
            "select id from transcript_utterances where text_tsv @@ plainto_tsquery('english', 'pricing expensive') and meeting_id = :mid"
        ),
        {"mid": meeting.id},
    ).first()
    assert row is not None

    db.delete(meeting)
    db.commit()

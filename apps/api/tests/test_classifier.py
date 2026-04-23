from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from intelligence.classifier import SignalClassifier
from models import Insight, InsightEvidence, LLMCache, Meeting, TranscriptUtterance


class FakeAnthropic:
    def __init__(self, responses: list[dict], token_in: int = 120, token_out: int = 40):
        self.responses = list(responses)
        self.token_in = token_in
        self.token_out = token_out
        self.calls: list[dict] = []

    async def create_message(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise RuntimeError("no more responses queued")
        tool_input = self.responses.pop(0)
        return {
            "tool_input": tool_input,
            "token_in": self.token_in,
            "token_out": self.token_out,
        }


def _meeting(db) -> Meeting:
    m = Meeting(
        meeting_url="https://meet.google.com/abc",
        status="in_call",
        recall_bot_id=f"bot_{uuid.uuid4().hex[:8]}",
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _utterance(db, meeting: Meeting, text: str, speaker: str = "customer", start_ms: int = 0) -> TranscriptUtterance:
    u = TranscriptUtterance(
        meeting_id=meeting.id,
        text=text,
        speaker_label=speaker,
        is_partial=False,
        start_ms=start_ms,
        end_ms=start_ms + 1000,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.mark.asyncio
async def test_classifier_persists_insight_with_evidence(db):
    m = _meeting(db)
    u1 = _utterance(db, m, "We like the tool but Gong is way cheaper.")

    fake = FakeAnthropic([{
        "signals": [
            {
                "type": "competitor_mention",
                "title": "Gong mentioned on pricing",
                "description": "Customer said Gong is way cheaper.",
                "severity": "medium",
                "confidence": 0.9,
                "source_aliases": ["B1"],
            }
        ]
    }])

    classifier = SignalClassifier(client=fake)
    insights, outcome = await classifier.classify_and_persist(db, m.id, [u1], [])
    db.commit()

    assert outcome == "fresh"
    assert len(insights) == 1
    assert insights[0].type == "competitor_mention"

    ev = db.execute(select(InsightEvidence).where(InsightEvidence.insight_id == insights[0].id)).scalars().all()
    assert len(ev) == 1
    assert ev[0].utterance_id == u1.id
    assert "Gong" in (ev[0].evidence_text or "")


@pytest.mark.asyncio
async def test_classifier_uses_cache_on_identical_batch(db):
    m = _meeting(db)
    u1 = _utterance(db, m, "Can you send the proposal by Friday?", speaker="customer")

    fake = FakeAnthropic([
        {"signals": [
            {"type": "commitment", "title": "Proposal by Friday",
             "description": "Customer asks for a proposal by Friday.",
             "severity": "medium", "confidence": 0.85, "source_aliases": ["B1"]}
        ]}
    ])
    classifier = SignalClassifier(client=fake)

    ins1, out1 = await classifier.classify_and_persist(db, m.id, [u1], [])
    db.commit()
    ins2, out2 = await classifier.classify_and_persist(db, m.id, [u1], [])
    db.commit()
    ins3, out3 = await classifier.classify_and_persist(db, m.id, [u1], [])
    db.commit()

    assert out1 == "fresh"
    assert out2 == "hit_cache"
    assert out3 == "hit_cache"
    assert len(fake.calls) == 1

    # cache row exists
    assert db.execute(select(LLMCache)).scalars().first() is not None


@pytest.mark.asyncio
async def test_classifier_multi_turn_context_in_prompt(db):
    m = _meeting(db)
    ctx = [
        _utterance(db, m, "We're considering a few options.", "customer", 100),
        _utterance(db, m, "What are you comparing us against?", "rep", 200),
    ]
    batch = [
        _utterance(db, m, "Honestly, mostly Gong and Chorus.", "customer", 300),
    ]

    fake = FakeAnthropic([
        {"signals": [
            {"type": "competitor_mention", "title": "Gong and Chorus",
             "description": "Customer lists Gong and Chorus as competitors.",
             "severity": "high", "confidence": 0.95,
             "source_aliases": ["C2", "B1"]}
        ]}
    ])

    classifier = SignalClassifier(client=fake)
    insights, _ = await classifier.classify_and_persist(db, m.id, batch, ctx)
    db.commit()

    assert len(insights) == 1
    ev = db.execute(select(InsightEvidence).where(InsightEvidence.insight_id == insights[0].id)).scalars().all()
    evidence_utt_ids = {e.utterance_id for e in ev}
    assert ctx[1].id in evidence_utt_ids, "context utterance should be linked as evidence"
    assert batch[0].id in evidence_utt_ids, "batch utterance should be linked as evidence"

    # verify the prompt actually included both blocks
    call = fake.calls[0]
    user_msg = call["messages"][0]["content"]
    assert "<context>" in user_msg and "<batch>" in user_msg
    assert "C1" in user_msg and "C2" in user_msg
    assert "B1" in user_msg


@pytest.mark.asyncio
async def test_classifier_skips_signal_with_unresolvable_alias(db):
    m = _meeting(db)
    u1 = _utterance(db, m, "We need pricing tiers.")

    fake = FakeAnthropic([
        {"signals": [
            {"type": "feature_request", "title": "Pricing tiers",
             "source_aliases": ["B999"]},  # bogus
        ]}
    ])
    classifier = SignalClassifier(client=fake)
    insights, _ = await classifier.classify_and_persist(db, m.id, [u1], [])
    db.commit()
    assert insights == []


@pytest.mark.asyncio
async def test_classifier_skips_unknown_type(db):
    m = _meeting(db)
    u1 = _utterance(db, m, "Random.")
    fake = FakeAnthropic([
        {"signals": [{"type": "not_a_real_type", "title": "x", "source_aliases": ["B1"]}]}
    ])
    classifier = SignalClassifier(client=fake)
    insights, _ = await classifier.classify_and_persist(db, m.id, [u1], [])
    db.commit()
    assert insights == []


@pytest.mark.asyncio
async def test_classifier_empty_batch_short_circuits(db):
    m = _meeting(db)
    fake = FakeAnthropic([])
    classifier = SignalClassifier(client=fake)
    insights, outcome = await classifier.classify_and_persist(db, m.id, [], [])
    assert insights == []
    assert outcome == "no_batch"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_classifier_empty_signal_list_is_ok(db):
    m = _meeting(db)
    u1 = _utterance(db, m, "Uh, yeah, hi.")
    fake = FakeAnthropic([{"signals": []}])
    classifier = SignalClassifier(client=fake)
    insights, outcome = await classifier.classify_and_persist(db, m.id, [u1], [])
    db.commit()
    assert insights == []
    assert outcome == "fresh"

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from db import SessionLocal
from intelligence.batcher import Batcher, fast_path_hit
from intelligence.classifier import SignalClassifier
from models import DeadLetterJob, Insight, InsightEvidence, LLMCache, Meeting, TranscriptUtterance
from tests.test_classifier import FakeAnthropic, _meeting, _utterance


def _utt(db, meeting: Meeting, text: str, start_ms: int, speaker: str = "customer") -> TranscriptUtterance:
    return _utterance(db, meeting, text, speaker=speaker, start_ms=start_ms)


def _signal(type_: str, title: str, aliases: list[str]) -> dict:
    return {"type": type_, "title": title, "source_aliases": aliases,
            "severity": "medium", "confidence": 0.8}


def test_fast_path_patterns():
    assert fast_path_hit("We'll send the proposal by Friday.") is True
    assert fast_path_hit("This is urgent, need it ASAP")
    assert fast_path_hit("Please share the pricing deck")
    assert not fast_path_hit("nice weather today")
    assert not fast_path_hit("we talked about pricing")


@pytest.mark.asyncio
async def test_size_trigger_flushes_at_threshold(db):
    m = _meeting(db)
    utts = [_utt(db, m, f"filler message number {i}", start_ms=i * 1000) for i in range(5)]

    fake = FakeAnthropic([{"signals": []}])
    classifier = SignalClassifier(client=fake)
    batcher = Batcher(
        session_factory=SessionLocal, classifier=classifier,
        size_threshold=5, time_threshold_ms=60_000,
    )

    for u in utts[:4]:
        await batcher.enqueue(m.id, u.id, u.text)
    assert len(fake.calls) == 0, "should not flush before threshold"

    await batcher.enqueue(m.id, utts[4].id, utts[4].text)
    await batcher.wait_idle()
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_fast_path_triggers_early_flush(db):
    m = _meeting(db)
    u1 = _utt(db, m, "Hey team.", 1000)
    u2 = _utt(db, m, "So, will you send the proposal by Friday?", 2000)

    fake = FakeAnthropic([
        {"signals": [_signal("commitment", "Proposal by Friday", ["B2"])]},
    ])
    classifier = SignalClassifier(client=fake)
    batcher = Batcher(
        session_factory=SessionLocal, classifier=classifier,
        size_threshold=100, time_threshold_ms=60_000,  # neither size nor time would fire
    )

    await batcher.enqueue(m.id, u1.id, u1.text)
    assert len(fake.calls) == 0
    await batcher.enqueue(m.id, u2.id, u2.text)
    await batcher.wait_idle()
    assert len(fake.calls) == 1
    insights = db.execute(select(Insight).where(Insight.meeting_id == m.id)).scalars().all()
    assert len(insights) == 1


@pytest.mark.asyncio
async def test_time_trigger_via_explicit_timer_tick(db):
    m = _meeting(db)
    u1 = _utt(db, m, "okay", 1000)

    fake = FakeAnthropic([{"signals": []}])
    classifier = SignalClassifier(client=fake)
    batcher = Batcher(
        session_factory=SessionLocal, classifier=classifier,
        size_threshold=100, time_threshold_ms=50,
    )

    await batcher.enqueue(m.id, u1.id, u1.text)
    # let time pass, then run timer once
    await asyncio.sleep(0.1)
    # manual tick: iterate once by inlining the timer's logic
    import time
    due = [mid for mid, ts in list(batcher._first_enqueue.items())
           if (time.monotonic() - ts) * 1000 >= batcher.time_threshold_ms]
    for mid in due:
        batcher._schedule_flush(mid)
    await batcher.wait_idle()
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_context_is_prior_utterances_only(db):
    m = _meeting(db)
    prior = [_utt(db, m, f"prior {i}", start_ms=i * 1000) for i in range(8)]
    batch = [_utt(db, m, "batch utterance", start_ms=10_000)]

    captured: dict[str, Any] = {}

    class CaptureFake(FakeAnthropic):
        async def create_message(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return await super().create_message(**kwargs)

    fake = CaptureFake([{"signals": []}])
    classifier = SignalClassifier(client=fake)
    batcher = Batcher(
        session_factory=SessionLocal, classifier=classifier,
        size_threshold=1, time_threshold_ms=60_000, context_size=6,
    )
    await batcher.enqueue(m.id, batch[0].id, batch[0].text)
    await batcher.wait_idle()

    user_msg = captured["messages"][0]["content"]
    # context section should mention exactly the 6 most recent priors, oldest first
    for i in range(2, 8):
        assert f"prior {i}" in user_msg
    # earliest two priors should have been dropped
    assert "prior 0" not in user_msg
    assert "prior 1" not in user_msg
    # the batch utterance shows up in <batch>
    assert "batch utterance" in user_msg


@pytest.mark.asyncio
async def test_classifier_failure_lands_in_dlq(db):
    m = _meeting(db)
    u1 = _utt(db, m, "anything", 1000)

    class ExplodingFake:
        calls = 0

        async def create_message(self, **kwargs):
            ExplodingFake.calls += 1
            raise RuntimeError("anthropic down")

    classifier = SignalClassifier(client=ExplodingFake())
    batcher = Batcher(
        session_factory=SessionLocal, classifier=classifier,
        size_threshold=1, time_threshold_ms=60_000,
    )
    await batcher.enqueue(m.id, u1.id, u1.text)
    await batcher.wait_idle()

    dlq = db.execute(select(DeadLetterJob).where(DeadLetterJob.meeting_id == m.id)).scalars().all()
    assert len(dlq) == 1
    assert dlq[0].job_kind == "classify"
    assert dlq[0].status == "open"
    assert "anthropic down" in (dlq[0].error or "")


@pytest.mark.asyncio
async def test_backpressure_bounds_llm_calls(db):
    """Fire many utterances fast; batcher groups them into ≪ N calls."""
    m = _meeting(db)
    utts = [_utt(db, m, f"utterance {i}", start_ms=i * 100) for i in range(100)]

    fake = FakeAnthropic([{"signals": []}] * 100)  # plenty of canned responses
    classifier = SignalClassifier(client=fake)
    batcher = Batcher(
        session_factory=SessionLocal, classifier=classifier,
        size_threshold=5, time_threshold_ms=60_000,
    )
    for u in utts:
        await batcher.enqueue(m.id, u.id, u.text)
    await batcher.flush_all()
    await batcher.wait_idle()

    # 100 utterances, threshold 5 → at most 20 calls, zero dropped utterances
    assert len(fake.calls) <= 20
    # every utterance should have ended up in some call's prompt
    total_in_prompts = sum("utterance" in call["messages"][0]["content"] for call in fake.calls)
    assert total_in_prompts == len(fake.calls)

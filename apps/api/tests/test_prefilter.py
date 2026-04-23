from __future__ import annotations

import pytest

from intelligence.prefilter import batch_likely_has_signal, likely_contains_signal


@pytest.mark.parametrize("text", [
    "We need Salesforce sync by Friday.",
    "Honestly Gong is about half the price.",
    "This is urgent, can you send the proposal ASAP?",
    "Our goal is to roll this out next quarter.",
    "We're looking for a cheaper option.",
    "Can you deliver by EOD?",
])
def test_positive_matches(text):
    assert likely_contains_signal(text) is True


@pytest.mark.parametrize("text", [
    "",
    "uh, yeah, hi",
    "Can you hear me? Give me one second.",
    "Sorry, my dog is barking",
    "Let me just take a quick drink of water",
    "Haha good one",
])
def test_negative_no_match(text):
    assert likely_contains_signal(text) is False


def test_batch_passes_if_any_hit():
    assert batch_likely_has_signal([
        "um yeah",
        "so anyway",
        "we need Salesforce integration",  # hit
    ]) is True


def test_batch_drops_if_all_filler():
    assert batch_likely_has_signal([
        "um yeah",
        "so anyway",
        "good good",
    ]) is False


def test_case_insensitive():
    assert likely_contains_signal("GONG is cheaper") is True
    assert likely_contains_signal("send the PROPOSAL") is True


@pytest.mark.asyncio
async def test_batcher_skips_llm_when_prefilter_drops(db):
    from db import SessionLocal
    from intelligence.batcher import Batcher
    from intelligence.classifier import SignalClassifier
    from models import Meeting, TranscriptUtterance
    from tests.test_classifier import FakeAnthropic

    import uuid as uuidlib

    m = Meeting(
        meeting_url="https://meet.google.com/x", status="in_call",
        recall_bot_id=f"bot_{uuidlib.uuid4().hex[:8]}",
    )
    db.add(m); db.commit(); db.refresh(m)

    utts = []
    for i, text in enumerate(["uh", "yeah", "hmm", "right", "good good"]):
        u = TranscriptUtterance(
            meeting_id=m.id, text=text, speaker_label="x",
            is_partial=False, start_ms=i * 1000, end_ms=i * 1000 + 500,
        )
        db.add(u); db.commit(); db.refresh(u)
        utts.append(u)

    fake = FakeAnthropic([{"signals": [{"type": "commitment", "title": "x", "source_aliases": ["B1"]}]}])
    classifier = SignalClassifier(client=fake)
    batcher = Batcher(
        session_factory=SessionLocal, classifier=classifier,
        size_threshold=5, time_threshold_ms=60_000,
    )

    for u in utts:
        await batcher.enqueue(m.id, u.id, u.text)
    await batcher.wait_idle()

    assert fake.calls == [], "LLM should not be called for filler-only batch"

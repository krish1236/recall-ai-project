from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from intelligence.synthesizer import Synthesizer
from models import ActionItem, Insight, Meeting, Summary, TranscriptUtterance
from tests.test_classifier import FakeAnthropic


def _meeting(db) -> Meeting:
    m = Meeting(
        title="Acme discovery", meeting_type="discovery",
        meeting_url="https://meet.google.com/x", status="processing",
        owner_name="rep@acme.com",
        recall_bot_id=f"bot_{uuid.uuid4().hex[:8]}",
    )
    db.add(m); db.commit(); db.refresh(m)
    return m


def _utt(db, meeting: Meeting, text: str, speaker: str, start_ms: int) -> TranscriptUtterance:
    u = TranscriptUtterance(
        meeting_id=meeting.id, text=text, speaker_label=speaker,
        is_partial=False, start_ms=start_ms, end_ms=start_ms + 1000,
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


def _insight(db, meeting: Meeting, type_: str, title: str) -> Insight:
    i = Insight(
        meeting_id=meeting.id, type=type_, title=title,
        severity="medium", confidence=0.8,
    )
    db.add(i); db.commit(); db.refresh(i)
    return i


CANNED = {
    "exec_summary": "Customer wants Salesforce sync and is considering Gong on price.",
    "risk_level": "medium",
    "action_items": [
        {"action_text": "Send proposal with Salesforce add-on pricing", "owner_name": "rep@acme.com", "due_hint": "Friday"},
        {"action_text": "Follow up with product on SF connector roadmap"},
    ],
    "followup_email": "Hi — following up on our conversation...\n- Salesforce sync is the key blocker\n- Proposal coming by Friday",
    "crm_note": "Customer evaluating Gong on price; SF sync is required; proposal by Friday.",
}


@pytest.mark.asyncio
async def test_synthesizer_persists_all_outputs(db):
    m = _meeting(db)
    _utt(db, m, "We need Salesforce sync.", "customer", 1000)
    _utt(db, m, "Got it. We can scope that.", "rep", 2000)
    _insight(db, m, "feature_request", "SF sync required")

    fake = FakeAnthropic([CANNED])
    synth = Synthesizer(client=fake)
    output, outcome = await synth.synthesize_and_persist(db, m.id)
    db.commit()

    assert outcome == "fresh"
    assert output is not None

    summaries = db.execute(select(Summary).where(Summary.meeting_id == m.id)).scalars().all()
    kinds = {s.summary_type for s in summaries}
    assert kinds == {"executive_summary", "followup_email", "crm_note"}

    actions = db.execute(select(ActionItem).where(ActionItem.meeting_id == m.id)).scalars().all()
    assert len(actions) == 2
    assert actions[0].action_text.startswith("Send proposal")
    assert actions[0].owner_name == "rep@acme.com"
    assert actions[0].due_hint == "Friday"


@pytest.mark.asyncio
async def test_synthesizer_idempotent_replace(db):
    m = _meeting(db)
    _utt(db, m, "hi", "rep", 0)
    fake = FakeAnthropic([CANNED, CANNED])
    synth = Synthesizer(client=fake)

    await synth.synthesize_and_persist(db, m.id); db.commit()
    count1 = db.execute(select(Summary).where(Summary.meeting_id == m.id)).scalars().all()
    assert len(count1) == 3

    await synth.synthesize_and_persist(db, m.id); db.commit()
    count2 = db.execute(select(Summary).where(Summary.meeting_id == m.id)).scalars().all()
    assert len(count2) == 3  # not 6 — prior rows were replaced


@pytest.mark.asyncio
async def test_synthesizer_skips_meeting_without_transcript(db):
    m = _meeting(db)
    fake = FakeAnthropic([CANNED])
    synth = Synthesizer(client=fake)
    output, outcome = await synth.synthesize_and_persist(db, m.id)
    assert output is None
    assert outcome == "no_transcript"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_synthesizer_404_for_missing_meeting(db):
    fake = FakeAnthropic([CANNED])
    synth = Synthesizer(client=fake)
    out, outcome = await synth.synthesize_and_persist(db, uuid.uuid4())
    assert out is None and outcome == "no_meeting"


@pytest.mark.asyncio
async def test_synthesizer_uses_cache_on_same_inputs(db):
    m = _meeting(db)
    _utt(db, m, "hello", "rep", 0)
    fake = FakeAnthropic([CANNED])  # only one response — second call must come from cache
    synth = Synthesizer(client=fake)

    _, out1 = await synth.synthesize_and_persist(db, m.id); db.commit()
    _, out2 = await synth.synthesize_and_persist(db, m.id); db.commit()
    assert out1 == "fresh"
    assert out2 == "hit_cache"
    assert len(fake.calls) == 1

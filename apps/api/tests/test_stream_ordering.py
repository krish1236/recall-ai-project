from __future__ import annotations

import json
import random
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tests._helpers import make_payload, svix_headers
from worker import Worker


def _ts(minute: int, second: int) -> str:
    return f"2026-04-22T10:{minute:02d}:{second:02d}+00:00"


def _fire_webhooks(timestamps: list[str], bot_id: str) -> None:
    from main import app
    c = TestClient(app)
    for ts in timestamps:
        payload = make_payload(bot_id=bot_id, event="transcript.data", timestamp=ts)
        raw = json.dumps(payload).encode()
        r = c.post("/webhook/recall", content=raw, headers=svix_headers(raw))
        assert r.status_code == 200, r.text


async def _drain(worker: Worker) -> None:
    for _ in range(50):
        n = await worker.poll_once(block_ms=0)
        if n == 0:
            break


@pytest.mark.asyncio
async def test_shuffled_events_processed_in_event_time_order(db):
    sorted_ts = [_ts(0, i) for i in range(50)]
    shuffled = list(sorted_ts)
    random.Random(1234).shuffle(shuffled)
    _fire_webhooks(shuffled, bot_id="bot_ordering_1")

    processed: list[datetime] = []

    async def track(event, session):
        processed.append(event.event_timestamp)

    worker = Worker(
        consumer_id="test-ordering-1",
        handlers={"transcript.data": track, "__default__": track},
        read_count=200,
    )
    await worker.start()
    try:
        await _drain(worker)
    finally:
        await worker.close()

    assert len(processed) == 50
    normalized = [ts.astimezone(timezone.utc) for ts in processed]
    assert normalized == sorted(normalized), "handler was not invoked in event_timestamp ASC"


@pytest.mark.asyncio
async def test_no_starvation_across_meetings(db):
    bots = [f"bot_par_{i}" for i in range(10)]
    for i, bot in enumerate(bots):
        _fire_webhooks([_ts(i, s) for s in range(10)], bot_id=bot)

    processed_by_bot: dict[str, list[datetime]] = {b: [] for b in bots}

    async def track(event, session):
        bot_id = (event.payload_json.get("data") or {}).get("bot_id")
        processed_by_bot.setdefault(bot_id, []).append(event.event_timestamp)

    worker = Worker(
        consumer_id="test-parallel-1",
        handlers={"transcript.data": track, "__default__": track},
        read_count=200,
    )
    await worker.start()
    try:
        await _drain(worker)
    finally:
        await worker.close()

    for bot in bots:
        assert len(processed_by_bot[bot]) == 10, f"{bot} got {len(processed_by_bot[bot])} events, expected 10"
        normalized = [ts.astimezone(timezone.utc) for ts in processed_by_bot[bot]]
        assert normalized == sorted(normalized), f"{bot} events were not timestamp-ordered"

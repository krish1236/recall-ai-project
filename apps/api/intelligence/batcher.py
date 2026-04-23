from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from intelligence.breaker import CircuitOpenError
from intelligence.classifier import SignalClassifier
from models import DeadLetterJob, TranscriptUtterance
from spans import mark_now

log = logging.getLogger("batcher")

# Fast-path: utterances containing any of these phrases flush immediately so
# urgent signals (commitments, risk language) surface without waiting for a
# size/time trigger.
_FAST_PATH_PATTERNS = [
    r"\b(will send|by (monday|tuesday|wednesday|thursday|friday|eod|end of day)|by next (week|month))\b",
    r"\b(urgent|asap|critical|blocker|right now|immediately)\b",
    r"\b(sounds good|let's do it|we have a deal|you have a deal|we're sold)\b",
    r"\b(can you (send|share|get|schedule)|please (send|share)|follow[- ]?up)\b",
]
_FAST_PATH = [re.compile(p, re.IGNORECASE) for p in _FAST_PATH_PATTERNS]


def fast_path_hit(text: str) -> bool:
    return any(p.search(text) for p in _FAST_PATH)


SessionFactory = Callable[[], Session]


class Batcher:
    """Per-meeting buffer with three flush triggers (size / time / fast-path)
    and a background timer. Designed so a single worker can feed many meetings
    concurrently without holding the LLM call on the critical path.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        classifier: SignalClassifier,
        *,
        size_threshold: int = 5,
        time_threshold_ms: int = 2500,
        context_size: int = 6,
        max_concurrent_flushes: int = 16,
    ) -> None:
        self.session_factory = session_factory
        self.classifier = classifier
        self.size_threshold = size_threshold
        self.time_threshold_ms = time_threshold_ms
        self.context_size = context_size

        self._buffers: dict[uuid.UUID, list[uuid.UUID]] = {}
        self._first_enqueue: dict[uuid.UUID, float] = {}
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._pending_tasks: set[asyncio.Task] = set()
        self._sem = asyncio.Semaphore(max_concurrent_flushes)

        self._stop = asyncio.Event()
        self._timer_task: Optional[asyncio.Task] = None

    def _lock(self, meeting_id: uuid.UUID) -> asyncio.Lock:
        lk = self._locks.get(meeting_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[meeting_id] = lk
        return lk

    async def enqueue(
        self,
        meeting_id: uuid.UUID,
        utterance_id: uuid.UUID,
        text: str,
    ) -> None:
        should_flush = False
        async with self._lock(meeting_id):
            buf = self._buffers.setdefault(meeting_id, [])
            if not buf:
                self._first_enqueue[meeting_id] = time.monotonic()
            buf.append(utterance_id)
            if len(buf) >= self.size_threshold or fast_path_hit(text or ""):
                should_flush = True
        session = self.session_factory()
        try:
            mark_now(session, utterance_id, "enqueued_at")
            session.commit()
        finally:
            session.close()
        if should_flush:
            self._schedule_flush(meeting_id)

    def _schedule_flush(self, meeting_id: uuid.UUID) -> None:
        task = asyncio.create_task(self.flush(meeting_id))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def flush(self, meeting_id: uuid.UUID) -> int:
        async with self._lock(meeting_id):
            batch_ids = self._buffers.pop(meeting_id, [])
            self._first_enqueue.pop(meeting_id, None)
        if not batch_ids:
            return 0

        async with self._sem:
            return await self._run_flush(meeting_id, batch_ids)

    async def _run_flush(
        self,
        meeting_id: uuid.UUID,
        batch_ids: list[uuid.UUID],
    ) -> int:
        session = self.session_factory()
        try:
            batch = [session.get(TranscriptUtterance, uid) for uid in batch_ids]
            batch = [u for u in batch if u is not None]
            if not batch:
                return 0
            earliest = min(u.start_ms or 0 for u in batch)
            context_rows = session.execute(
                select(TranscriptUtterance)
                .where(
                    TranscriptUtterance.meeting_id == meeting_id,
                    TranscriptUtterance.id.notin_(batch_ids),
                    TranscriptUtterance.is_partial.is_(False),
                    TranscriptUtterance.start_ms < earliest,
                )
                .order_by(TranscriptUtterance.start_ms.desc())
                .limit(self.context_size)
            ).scalars().all()
            context = list(reversed(context_rows))

            try:
                insights, outcome = await self.classifier.classify_and_persist(
                    session, meeting_id, batch, context,
                )
                for u in batch:
                    mark_now(session, u.id, "classified_at")
                session.commit()
                log.info(
                    "flushed meeting=%s batch=%d ctx=%d insights=%d outcome=%s",
                    meeting_id, len(batch), len(context), len(insights), outcome,
                )
                if insights:
                    from streams import publish_live
                    await publish_live(meeting_id, "insights", {
                        "insights": [
                            {
                                "id": str(i.id),
                                "type": i.type,
                                "title": i.title,
                                "description": i.description,
                                "severity": i.severity,
                                "confidence": float(i.confidence) if i.confidence is not None else None,
                            }
                            for i in insights
                        ],
                    })
                return len(insights)
            except Exception as e:  # noqa: BLE001
                session.rollback()
                is_circuit_open = isinstance(e, CircuitOpenError)
                if is_circuit_open:
                    log.info(
                        "classifier circuit-open for meeting=%s, batch→DLQ: %s",
                        meeting_id, e,
                    )
                else:
                    log.exception("classifier failed for meeting=%s: %s", meeting_id, e)
                session.add(DeadLetterJob(
                    job_kind="classify",
                    meeting_id=meeting_id,
                    payload_json={
                        "batch_utterance_ids": [str(u) for u in batch_ids],
                    },
                    error=str(e)[:1000],
                    attempt_count=1,
                    status="circuit_open" if is_circuit_open else "open",
                ))
                session.commit()
                return 0
        finally:
            session.close()

    async def flush_all(self) -> int:
        meeting_ids = list(self._buffers.keys())
        totals = 0
        for mid in meeting_ids:
            totals += await self.flush(mid)
        await self.wait_idle()
        return totals

    async def wait_idle(self) -> None:
        while self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)

    async def run_timer(self, interval_s: float = 0.5) -> None:
        self._timer_task = asyncio.current_task()
        while not self._stop.is_set():
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                return
            now = time.monotonic()
            due: list[uuid.UUID] = []
            for mid, first in list(self._first_enqueue.items()):
                if (now - first) * 1000 >= self.time_threshold_ms:
                    due.append(mid)
            for mid in due:
                self._schedule_flush(mid)

    def stop(self) -> None:
        self._stop.set()

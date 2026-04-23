from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy.orm import Session

from db import SessionLocal
from handlers import DEFAULT_HANDLERS, build_handlers, handle_unknown
from models import DeadLetterJob, MeetingEvent
from streams import (
    GROUP_NAME,
    ensure_group,
    list_active_streams,
    make_client,
    stream_key,
)

log = logging.getLogger("worker")


def _parse_ts(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


Handler = Callable[[MeetingEvent, Session], Awaitable[None]]


class Worker:
    def __init__(
        self,
        consumer_id: str = "worker-1",
        handlers: Optional[dict[str, Handler]] = None,
        max_attempts: int = 3,
        read_count: int = 64,
        batcher=None,
    ) -> None:
        self.consumer_id = consumer_id
        self.handlers = handlers if handlers is not None else DEFAULT_HANDLERS
        self.max_attempts = max_attempts
        self.read_count = read_count
        self.stop_event = asyncio.Event()
        self.r = None  # type: ignore[assignment]
        self.batcher = batcher
        self._batcher_timer: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self.r = make_client()
        if self.batcher is not None:
            self._batcher_timer = asyncio.create_task(self.batcher.run_timer(0.5))

    async def close(self) -> None:
        if self.batcher is not None:
            self.batcher.stop()
            if self._batcher_timer is not None:
                self._batcher_timer.cancel()
                try:
                    await self._batcher_timer
                except asyncio.CancelledError:
                    pass
                self._batcher_timer = None
            await self.batcher.flush_all()
        if self.r is not None:
            await self.r.aclose()
            self.r = None

    def request_stop(self) -> None:
        self.stop_event.set()

    def _route(self, event_type: str) -> Handler:
        if event_type in self.handlers:
            return self.handlers[event_type]
        for prefix, h in self.handlers.items():
            if prefix == "__default__":
                continue
            if event_type.startswith(prefix + "."):
                return h
        return self.handlers.get("__default__", handle_unknown)

    async def poll_once(self, block_ms: int = 1000) -> int:
        assert self.r is not None, "call start() first"
        streams = await list_active_streams(self.r)
        if not streams:
            if block_ms > 0:
                await asyncio.sleep(block_ms / 1000)
            return 0
        stream_names = [stream_key(b) for b in streams]
        for s in stream_names:
            await ensure_group(self.r, s)
        # block=None → non-blocking (don't send BLOCK at all); Redis treats BLOCK 0 as infinite.
        kwargs: dict = {"count": self.read_count}
        if block_ms and block_ms > 0:
            kwargs["block"] = block_ms
        try:
            result = await self.r.xreadgroup(
                GROUP_NAME,
                self.consumer_id,
                {s: ">" for s in stream_names},
                **kwargs,
            )
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "NOGROUP" not in msg:
                raise
            # stale entry in streams:active: the stream was dropped (e.g. test flushdb)
            # but the bot_id lingered in the set. Drop the offenders and retry next poll.
            for name, bot in zip(stream_names, streams):
                if not await self.r.exists(name):
                    log.info("pruning stale stream=%s from active set", name)
                    await self.r.srem("streams:active", bot)
            return 0
        if not result:
            return 0

        entries: list[tuple[str, str, dict, datetime]] = []
        for stream_name, ents in result:
            for entry_id, fields in ents:
                ts = _parse_ts(fields.get("event_timestamp", ""))
                entries.append((stream_name, entry_id, fields, ts))
        # event-time ordering across all streams read in this batch (HP-4)
        entries.sort(key=lambda e: e[3])

        processed = 0
        for stream_name, entry_id, fields, _ts in entries:
            await self.process_entry(stream_name, entry_id, fields)
            processed += 1
        return processed

    async def process_entry(self, stream_name: str, entry_id: str, fields: dict) -> None:
        assert self.r is not None
        event_db_id = int(fields["event_id"])
        event_type = fields.get("event_type", "")
        with SessionLocal() as session:
            event = session.get(MeetingEvent, event_db_id)
            if event is None:
                await self.r.xack(stream_name, GROUP_NAME, entry_id)
                return
            handler = self._route(event_type)
            attempts = 0
            last_err: Optional[Exception] = None
            while attempts < self.max_attempts:
                try:
                    await handler(event, session)
                    session.commit()
                    await self.r.xack(stream_name, GROUP_NAME, entry_id)
                    return
                except Exception as e:  # noqa: BLE001
                    session.rollback()
                    attempts += 1
                    last_err = e
                    log.warning("handler %s failed attempt %d: %s", event_type, attempts, e)
            session.add(DeadLetterJob(
                job_kind=f"stream_handler:{event_type}",
                meeting_id=event.meeting_id,
                payload_json={"event_id": event_db_id, "event_type": event_type, "stream": stream_name},
                error=str(last_err) if last_err else "unknown",
                attempt_count=attempts,
                status="open",
            ))
            session.commit()
            await self.r.xack(stream_name, GROUP_NAME, entry_id)

    async def run(self) -> None:
        await self.start()
        try:
            while not self.stop_event.is_set():
                try:
                    await self.poll_once(block_ms=1000)
                except Exception as e:  # noqa: BLE001
                    log.exception("poll loop error: %s", e)
                    await asyncio.sleep(1.0)
        finally:
            await self.close()


async def _main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

    from intelligence.batcher import Batcher
    from intelligence.classifier import AnthropicClient, SignalClassifier

    client = AnthropicClient()
    classifier = SignalClassifier(client=client)
    batcher = Batcher(session_factory=SessionLocal, classifier=classifier)
    handlers = build_handlers(batcher=batcher)

    worker = Worker(
        consumer_id=f"worker-{os.getpid()}",
        handlers=handlers,
        batcher=batcher,
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except NotImplementedError:
            pass
    log.info("worker started consumer=%s", worker.consumer_id)
    await worker.run()
    log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(_main())

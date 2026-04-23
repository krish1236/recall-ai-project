from __future__ import annotations

import os
from datetime import datetime

import redis.asyncio as redis_async

ACTIVE_STREAMS_SET = "streams:active"
GROUP_NAME = "workers"


def stream_key(bot_id: str) -> str:
    return f"stream:meeting:{bot_id}"


def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:56379")


def make_client() -> redis_async.Redis:
    return redis_async.from_url(redis_url(), decode_responses=True)


async def dispatch_event(
    bot_id: str,
    event_db_id: int,
    event_type: str,
    event_timestamp: datetime,
) -> None:
    """Called by the webhook receiver after a successful meeting_events insert.

    Each call opens + closes its own Redis client so it stays safe under
    short-lived event loops (e.g. starlette TestClient's per-request portal).
    Production FastAPI still works because the cost is trivial compared to
    the DB write that preceded it.
    """
    r = make_client()
    try:
        pipe = r.pipeline()
        pipe.sadd(ACTIVE_STREAMS_SET, bot_id)
        pipe.xadd(
            stream_key(bot_id),
            {
                "event_id": str(event_db_id),
                "event_type": event_type,
                "event_timestamp": event_timestamp.isoformat(),
                "bot_id": bot_id,
            },
        )
        await pipe.execute()
    finally:
        await r.aclose()


async def list_active_streams(r: redis_async.Redis) -> list[str]:
    members = await r.smembers(ACTIVE_STREAMS_SET)
    return sorted(members)


async def ensure_group(r: redis_async.Redis, stream: str) -> None:
    try:
        await r.xgroup_create(stream, GROUP_NAME, id="0", mkstream=True)
    except redis_async.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise

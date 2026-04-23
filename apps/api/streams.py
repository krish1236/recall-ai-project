from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any

import redis.asyncio as redis_async

ACTIVE_STREAMS_SET = "streams:active"
GROUP_NAME = "workers"


def stream_key(bot_id: str) -> str:
    return f"stream:meeting:{bot_id}"


def live_channel(meeting_id: uuid.UUID | str) -> str:
    return f"live:{meeting_id}"


def _default_encode(o: Any) -> Any:
    if isinstance(o, (uuid.UUID,)):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"can't encode {type(o).__name__}")


async def publish_live(meeting_id: uuid.UUID | str, event_type: str, payload: dict) -> None:
    """Fire a live-update frame onto the meeting's pub/sub channel so any
    connected WebSocket subscriber sees it. Best-effort — publish failures are
    swallowed since they must never block the committing write path."""
    message = {"type": event_type, **payload}
    r = make_client()
    try:
        await r.publish(
            live_channel(meeting_id),
            json.dumps(message, default=_default_encode),
        )
    except Exception:  # noqa: BLE001
        pass
    finally:
        await r.aclose()


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

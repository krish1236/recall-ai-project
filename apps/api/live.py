from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from streams import live_channel, make_client

log = logging.getLogger("live")

router = APIRouter(tags=["live"])


@router.websocket("/live/{meeting_id}")
async def live_meeting(websocket: WebSocket, meeting_id: UUID) -> None:
    await websocket.accept()
    channel = live_channel(meeting_id)
    r = make_client()
    pubsub = r.pubsub()
    await pubsub.subscribe(channel)

    # initial frame so the client knows we're connected
    try:
        await websocket.send_json({"type": "connected", "meeting_id": str(meeting_id)})
    except Exception:
        await pubsub.unsubscribe(channel)
        await pubsub.close()
        await r.aclose()
        return

    async def client_listener() -> None:
        # drain anything the client sends so the connection stays clean
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return

    listener_task = asyncio.create_task(client_listener())
    try:
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue
            try:
                await websocket.send_text(msg["data"])
            except Exception:
                break
            if listener_task.done():
                break
    except Exception as e:  # noqa: BLE001
        log.info("live ws loop ended: %s", e)
    finally:
        listener_task.cancel()
        try:
            await pubsub.unsubscribe(channel)
        except Exception:  # noqa: BLE001
            pass
        try:
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            pass
        await r.aclose()
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass

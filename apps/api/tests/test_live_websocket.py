from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from streams import publish_live


@pytest.mark.asyncio
async def test_websocket_receives_published_frames():
    from main import app
    mid = uuid.uuid4()

    with TestClient(app) as client:
        with client.websocket_connect(f"/live/{mid}") as ws:
            hello = ws.receive_json()
            assert hello == {"type": "connected", "meeting_id": str(mid)}

            await publish_live(mid, "utterance", {
                "id": "u-abc",
                "speaker_label": "customer",
                "text": "We need Salesforce sync.",
            })

            frame = json.loads(ws.receive_text())
            assert frame["type"] == "utterance"
            assert frame["speaker_label"] == "customer"
            assert "Salesforce" in frame["text"]


@pytest.mark.asyncio
async def test_websocket_only_receives_matching_meeting():
    from main import app
    mid_a = uuid.uuid4()
    mid_b = uuid.uuid4()

    with TestClient(app) as client:
        with client.websocket_connect(f"/live/{mid_a}") as ws_a:
            ws_a.receive_json()  # hello frame

            # publish on B; A should not see it
            await publish_live(mid_b, "utterance", {"id": "x", "text": "other meeting"})
            await publish_live(mid_a, "utterance", {"id": "y", "text": "correct meeting"})

            frame = json.loads(ws_a.receive_text())
            assert frame["text"] == "correct meeting"

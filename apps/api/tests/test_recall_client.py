from __future__ import annotations

import json

import httpx
import pytest

from recall_client import DEFAULT_REALTIME_EVENTS, RecallClient, RecallError


def _transport_for(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_create_bot_sends_auth_and_webhook_config():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "bot_abc_123", "status": "bot_requested"})

    http = httpx.AsyncClient(transport=_transport_for(handler))
    client = RecallClient(api_key="tok_test", region="us-east-1", http=http)

    result = await client.create_bot(
        meeting_url="https://meet.google.com/abc",
        webhook_url="https://example.com/webhook/recall",
        webhook_events=DEFAULT_REALTIME_EVENTS,
        bot_name="demo-bot",
    )

    assert result == {"id": "bot_abc_123", "status": "bot_requested"}
    assert captured["url"] == "https://us-east-1.recall.ai/api/v1/bot"
    assert captured["method"] == "POST"
    assert captured["auth"] == "Token tok_test"
    body = captured["body"]
    assert body["meeting_url"] == "https://meet.google.com/abc"
    assert body["bot_name"] == "demo-bot"
    realtime = body["recording_config"]["realtime_endpoints"][0]
    assert realtime["type"] == "webhook"
    assert realtime["url"] == "https://example.com/webhook/recall"
    assert "transcript.data" in realtime["events"]


@pytest.mark.asyncio
async def test_get_bot_uses_bot_id_in_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://us-east-1.recall.ai/api/v1/bot/bot_abc_123"
        return httpx.Response(200, json={"id": "bot_abc_123", "status": "in_call_recording"})

    http = httpx.AsyncClient(transport=_transport_for(handler))
    client = RecallClient(api_key="tok_test", region="us-east-1", http=http)
    result = await client.get_bot("bot_abc_123")
    assert result["status"] == "in_call_recording"


@pytest.mark.asyncio
async def test_4xx_raises_recall_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid token"})

    http = httpx.AsyncClient(transport=_transport_for(handler))
    client = RecallClient(api_key="bad", region="us-east-1", http=http)

    with pytest.raises(RecallError) as exc:
        await client.create_bot("url", "hook", [])
    assert exc.value.status_code == 401
    assert exc.value.body == {"detail": "invalid token"}


@pytest.mark.asyncio
async def test_region_determines_base_url():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"id": "x"})

    http = httpx.AsyncClient(transport=_transport_for(handler))
    client = RecallClient(api_key="t", region="eu-central-1", http=http)
    await client.get_bot("x")
    assert seen[0].startswith("https://eu-central-1.recall.ai/api/v1/bot/")

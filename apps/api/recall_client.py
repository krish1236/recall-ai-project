from __future__ import annotations

import os
from typing import Any, Optional

import httpx


class RecallError(Exception):
    def __init__(self, status_code: int, message: str, body: Any = None):
        super().__init__(f"recall api error {status_code}: {message}")
        self.status_code = status_code
        self.body = body


class RecallClient:
    def __init__(
        self,
        api_key: str,
        region: str,
        http: Optional[httpx.AsyncClient] = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.region = region
        self.base_url = f"https://{region}.recall.ai/api/v1"
        self._http = http
        self._timeout = timeout_s

    @classmethod
    def from_env(cls, http: Optional[httpx.AsyncClient] = None) -> "RecallClient":
        return cls(
            api_key=os.environ.get("RECALL_API_KEY", ""),
            region=os.environ.get("RECALL_REGION", "us-east-1"),
            http=http,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, json: Any = None) -> dict:
        url = f"{self.base_url}{path}"
        owns_client = self._http is None
        client = self._http or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.request(method, url, headers=self._headers(), json=json)
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                raise RecallError(resp.status_code, resp.text[:500], body)
            return resp.json()
        finally:
            if owns_client:
                await client.aclose()

    async def create_bot(
        self,
        meeting_url: str,
        webhook_url: str,
        webhook_events: list[str],
        bot_name: str = "Recall Demo Bot",
    ) -> dict:
        body = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "recording_config": {
                "transcript": {
                    "provider": {"meeting_captions": {}},
                },
                "realtime_endpoints": [
                    {
                        "type": "webhook",
                        "url": webhook_url,
                        "events": webhook_events,
                    }
                ],
            },
        }
        return await self._request("POST", "/bot", json=body)

    async def get_bot(self, bot_id: str) -> dict:
        return await self._request("GET", f"/bot/{bot_id}")


# Recall's realtime_endpoints only accept in-call events. Bot lifecycle
# (requested → joining → in_call → done | fatal) is delivered via a separate
# bot-status webhook configured independently; for now we drive state changes
# from the in-call participant/transcript activity we already receive.
DEFAULT_REALTIME_EVENTS = [
    "transcript.data",
    "transcript.partial_data",
    "participant_events.join",
    "participant_events.leave",
    "participant_events.chat_message",
]

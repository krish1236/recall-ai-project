from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

TEST_SECRET = "test-webhook-secret-do-not-use-in-prod"


def sign(raw: bytes, secret: str = TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def make_payload(
    bot_id: str = "bot_fake_001",
    event: str = "transcript.data",
    timestamp: str | None = None,
    text: str = "hello world",
    speaker: str = "customer",
) -> dict[str, Any]:
    ts = timestamp or datetime.now(tz=timezone.utc).isoformat()
    return {
        "event": event,
        "timestamp": ts,
        "data": {
            "bot_id": bot_id,
            "words": [{"text": text, "speaker": speaker, "start": 0.0, "end": 1.0}],
        },
    }


def canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

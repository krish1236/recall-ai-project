from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

# test secret in whsec_<base64> form so it matches real Recall/Svix format
_TEST_RAW = b"test-webhook-secret-do-not-use-in-prod"
TEST_SECRET = "whsec_" + base64.b64encode(_TEST_RAW).decode()


def _decode_secret(secret: str) -> bytes:
    assert secret.startswith("whsec_"), "svix-format secret required"
    return base64.b64decode(secret[len("whsec_"):])


def svix_headers(
    raw: bytes,
    secret: str = TEST_SECRET,
    msg_id: str | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    msg_id = msg_id or f"msg_{uuid.uuid4().hex}"
    ts = str(timestamp if timestamp is not None else int(time.time()))
    signed = f"{msg_id}.{ts}.{raw.decode()}".encode()
    sig = base64.b64encode(
        hmac.new(_decode_secret(secret), signed, hashlib.sha256).digest()
    ).decode()
    return {
        "svix-id": msg_id,
        "svix-timestamp": ts,
        "svix-signature": f"v1,{sig}",
        "content-type": "application/json",
    }


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


# kept for backwards-compat tests that may still import `sign`
def sign(raw: bytes, secret: str = TEST_SECRET) -> str:
    """Legacy name; returns the v1 svix signature (without the extra headers)."""
    return svix_headers(raw, secret)["svix-signature"]

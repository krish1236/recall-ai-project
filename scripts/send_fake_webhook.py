"""
Send a fake signed Recall webhook to the local ingest endpoint.

Usage:
    python scripts/send_fake_webhook.py [--url URL] [--bot BOT_ID]
                                        [--event EVENT_TYPE] [--count N]
                                        [--secret SECRET]

Reads RECALL_WEBHOOK_SECRET from env if --secret not given.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib import request


def sign(raw: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def build_payload(bot_id: str, event: str, text: str) -> dict:
    return {
        "event": event,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "data": {
            "bot_id": bot_id,
            "words": [
                {"text": text, "speaker": "customer", "start": 0.0, "end": 1.2}
            ],
        },
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000/webhook/recall")
    p.add_argument("--bot", default="bot_fake_001")
    p.add_argument("--event", default="transcript.data")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--text", default="hello from the fake sender")
    p.add_argument("--secret", default=os.environ.get("RECALL_WEBHOOK_SECRET", ""))
    args = p.parse_args()

    if not args.secret:
        print("error: RECALL_WEBHOOK_SECRET not set and --secret not given", file=sys.stderr)
        return 2

    accepted = duplicate = failed = 0
    for i in range(args.count):
        payload = build_payload(args.bot, args.event, f"{args.text} ({i})")
        raw = json.dumps(payload).encode()
        req = request.Request(
            args.url,
            data=raw,
            headers={
                "content-type": "application/json",
                "x-recall-signature": sign(raw, args.secret),
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode())
                status = body.get("status")
                if status == "accepted":
                    accepted += 1
                elif status == "duplicate":
                    duplicate += 1
                print(f"[{i+1}/{args.count}] {resp.status} {status} {body.get('event_type')}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[{i+1}/{args.count}] failed: {e}")
        time.sleep(0.05)

    print(f"\naccepted={accepted} duplicate={duplicate} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

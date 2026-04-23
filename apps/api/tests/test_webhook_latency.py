from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from tests._helpers import make_payload, svix_headers


def _client():
    from main import app
    return TestClient(app)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    k = int(len(sorted_v) * p / 100)
    k = min(k, len(sorted_v) - 1)
    return sorted_v[k]


def test_ingest_p99_under_budget(db):
    c = _client()
    n = 1000
    latencies_ms: list[float] = []

    for i in range(n):
        payload = make_payload(
            bot_id="bot_latency",
            event="transcript.data",
            timestamp=f"2026-04-22T10:00:{i:02d}+00:00",
            text=f"utterance {i}",
        )
        raw = json.dumps(payload).encode()
        headers = svix_headers(raw)

        t0 = time.perf_counter()
        r = c.post("/webhook/recall", content=raw, headers=headers)
        latencies_ms.append((time.perf_counter() - t0) * 1000)
        assert r.status_code == 200

    p50 = _percentile(latencies_ms, 50)
    p95 = _percentile(latencies_ms, 95)
    p99 = _percentile(latencies_ms, 99)
    print(f"\ningest latency over {n} requests: p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms")

    assert p99 < 50.0, f"p99 {p99:.1f}ms exceeded 50ms budget"

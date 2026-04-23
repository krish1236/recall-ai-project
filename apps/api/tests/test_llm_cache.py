from __future__ import annotations

import pytest

from intelligence.cache import compute_cache_key, get_or_call, pricing_usd
from models import LLMCache


def test_cache_key_is_deterministic():
    k1 = compute_cache_key("claude-haiku", ["system", "user"], 0.2, {"name": "t"})
    k2 = compute_cache_key("claude-haiku", ["system", "user"], 0.2, {"name": "t"})
    assert k1 == k2
    assert k1.startswith("sha256:")


def test_cache_key_changes_with_any_input():
    base = compute_cache_key("claude-haiku", ["a"], 0.2, {})
    assert base != compute_cache_key("claude-sonnet", ["a"], 0.2, {})
    assert base != compute_cache_key("claude-haiku", ["a", "b"], 0.2, {})
    assert base != compute_cache_key("claude-haiku", ["a"], 0.3, {})
    assert base != compute_cache_key("claude-haiku", ["a"], 0.2, {"x": 1})


def test_pricing_is_monotonic_in_tokens():
    assert pricing_usd("claude-haiku", 100, 100) < pricing_usd("claude-haiku", 200, 200)
    assert pricing_usd("claude-sonnet", 100, 100) > pricing_usd("claude-haiku", 100, 100)


@pytest.mark.asyncio
async def test_get_or_call_caches_after_first_miss(db):
    key = compute_cache_key("claude-haiku", ["hello"], 0.0, None)
    call_count = 0

    async def caller():
        nonlocal call_count
        call_count += 1
        return {
            "response": {"insights": [{"type": "objection", "title": "pricing"}]},
            "token_in": 120,
            "token_out": 30,
            "cost_usd": 0.00027,
        }

    resp1, outcome1 = await get_or_call(db, key, caller)
    resp2, outcome2 = await get_or_call(db, key, caller)
    resp3, outcome3 = await get_or_call(db, key, caller)

    assert outcome1 == "fresh"
    assert outcome2 == "hit_cache"
    assert outcome3 == "hit_cache"
    assert call_count == 1
    assert resp1 == resp2 == resp3

    row = db.get(LLMCache, key)
    assert row is not None
    assert row.token_in == 120
    assert row.token_out == 30


@pytest.mark.asyncio
async def test_different_keys_each_call_caller(db):
    count = 0

    async def caller():
        nonlocal count
        count += 1
        return {"response": {"n": count}, "token_in": 0, "token_out": 0, "cost_usd": 0.0}

    for i in range(5):
        key = compute_cache_key("claude-haiku", [f"prompt-{i}"], 0.0, None)
        _, outcome = await get_or_call(db, key, caller)
        assert outcome == "fresh"

    assert count == 5

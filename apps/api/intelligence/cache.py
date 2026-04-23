from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from models import LLMCache


def compute_cache_key(
    model: str,
    prompt_parts: list[str],
    temperature: float,
    tool_schema: Optional[dict] = None,
) -> str:
    """Deterministic prompt-hash key. Stable across process restarts."""
    material = {
        "model": model,
        "prompt": prompt_parts,
        "temperature": round(float(temperature), 4),
        "tool_schema": tool_schema or {},
    }
    canon = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode()).hexdigest()


async def get_or_call(
    session: Session,
    cache_key: str,
    caller: Callable[[], Awaitable[dict]],
) -> tuple[dict, str]:
    """Return (response, outcome) where outcome is 'hit_cache' | 'fresh'.

    Caller must return a dict of shape:
      {"response": <json>, "token_in": int, "token_out": int, "cost_usd": float}
    """
    existing = session.get(LLMCache, cache_key)
    if existing is not None:
        return existing.response_json, "hit_cache"

    result = await caller()
    response = result["response"]
    # Upsert — two concurrent flushes with the same prompt shouldn't error
    stmt = (
        pg_insert(LLMCache)
        .values(
            cache_key=cache_key,
            response_json=response,
            token_in=int(result.get("token_in", 0)),
            token_out=int(result.get("token_out", 0)),
            cost_usd=Decimal(str(result.get("cost_usd", 0))),
        )
        .on_conflict_do_nothing(index_elements=["cache_key"])
    )
    session.execute(stmt)
    session.commit()
    return response, "fresh"


def pricing_usd(model: str, token_in: int, token_out: int) -> float:
    """Rough per-token pricing so we can track spend per meeting.

    Prices in USD per 1M tokens. These are approximate — worth hard-coding for
    cost tracking but not for billing customers.
    """
    rates = {
        "claude-haiku-4-5-20251001": (1.0, 5.0),
        "claude-haiku": (1.0, 5.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-sonnet": (3.0, 15.0),
    }
    rate_in, rate_out = rates.get(model, (1.0, 5.0))
    return (token_in / 1_000_000.0) * rate_in + (token_out / 1_000_000.0) * rate_out

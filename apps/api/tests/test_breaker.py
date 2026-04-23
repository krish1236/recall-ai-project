from __future__ import annotations

import asyncio

import pytest

from intelligence.breaker import CircuitBreaker, CircuitOpenError


class _Flaky:
    def __init__(self, fail_first: int) -> None:
        self.fail_first = fail_first
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RuntimeError("boom")
        return "ok"


@pytest.mark.asyncio
async def test_closes_when_all_calls_succeed():
    b = CircuitBreaker(name="test", min_samples=3, window_size=5, open_seconds=0.05)
    target = _Flaky(fail_first=0)
    for _ in range(5):
        assert await b.call(target) == "ok"
    assert b.state == "closed"


@pytest.mark.asyncio
async def test_opens_after_threshold_pct_breached():
    b = CircuitBreaker(name="test", min_samples=5, threshold_pct=30.0, window_size=10, open_seconds=60.0)
    target = _Flaky(fail_first=1000)
    # send 5 failing calls, should open
    for _ in range(5):
        with pytest.raises(RuntimeError):
            await b.call(target)
    assert b.state == "open"


@pytest.mark.asyncio
async def test_raises_circuit_open_while_open():
    b = CircuitBreaker(name="test", min_samples=3, threshold_pct=30.0, window_size=5, open_seconds=60.0)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await b.call(_Flaky(fail_first=1000))
    assert b.state == "open"
    with pytest.raises(CircuitOpenError):
        await b.call(_Flaky(fail_first=0))


@pytest.mark.asyncio
async def test_half_open_closes_on_success():
    b = CircuitBreaker(name="test", min_samples=3, threshold_pct=30.0, window_size=5, open_seconds=0.02)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await b.call(_Flaky(fail_first=1000))
    assert b.state == "open"
    await asyncio.sleep(0.05)
    # half-open probe succeeds → close
    assert await b.call(_Flaky(fail_first=0)) == "ok"
    assert b.state == "closed"


@pytest.mark.asyncio
async def test_half_open_reopens_on_failure():
    b = CircuitBreaker(name="test", min_samples=3, threshold_pct=30.0, window_size=5, open_seconds=0.02)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await b.call(_Flaky(fail_first=1000))
    assert b.state == "open"
    await asyncio.sleep(0.05)
    # half-open probe fails → re-open
    with pytest.raises(RuntimeError):
        await b.call(_Flaky(fail_first=10))
    assert b.state == "open"

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable, TypeVar

log = logging.getLogger("breaker")

T = TypeVar("T")


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the breaker is open."""

    def __init__(self, name: str, reopen_in_s: float):
        super().__init__(f"circuit '{name}' is open; retry in {reopen_in_s:.1f}s")
        self.name = name
        self.reopen_in_s = reopen_in_s


class CircuitBreaker:
    """Rolling-window error-rate breaker.

    States: closed → open → half_open → closed/open.
    While open, calls raise CircuitOpenError without invoking the callable.
    After ``open_seconds`` the next call becomes a half-open probe; success
    closes, failure re-opens for another window.
    """

    def __init__(
        self,
        name: str = "llm",
        *,
        threshold_pct: float = 30.0,
        min_samples: int = 10,
        window_size: int = 30,
        open_seconds: float = 30.0,
    ) -> None:
        self.name = name
        self.threshold_pct = threshold_pct
        self.min_samples = min_samples
        self.window_size = window_size
        self.open_seconds = open_seconds
        self._outcomes: deque[bool] = deque(maxlen=window_size)
        self._state: str = "closed"
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state

    def reopen_in_s(self) -> float:
        return max(0.0, (self._opened_at + self.open_seconds) - time.monotonic())

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            self._maybe_transition_to_half_open()
            if self._state == "open":
                raise CircuitOpenError(self.name, self.reopen_in_s())

        try:
            result = await fn()
        except Exception:
            async with self._lock:
                self._record(False)
            raise
        else:
            async with self._lock:
                self._record(True)
            return result

    def _record(self, ok: bool) -> None:
        self._outcomes.append(ok)
        if self._state == "half_open":
            if ok:
                self._close()
            else:
                self._open()
            return
        if self._state == "closed" and len(self._outcomes) >= self.min_samples:
            failures = sum(1 for x in self._outcomes if not x)
            pct = 100.0 * failures / len(self._outcomes)
            if pct >= self.threshold_pct:
                self._open()

    def _maybe_transition_to_half_open(self) -> None:
        if self._state == "open" and self.reopen_in_s() <= 0:
            log.info("breaker '%s' half-open (probing)", self.name)
            self._state = "half_open"

    def _open(self) -> None:
        self._state = "open"
        self._opened_at = time.monotonic()
        log.warning(
            "breaker '%s' OPEN — %d failures in last %d",
            self.name, sum(1 for x in self._outcomes if not x), len(self._outcomes),
        )

    def _close(self) -> None:
        self._state = "closed"
        self._outcomes.clear()
        log.info("breaker '%s' CLOSED", self.name)


_REGISTRY: dict[str, CircuitBreaker] = {}


def get_breaker(name: str = "llm", **kwargs) -> CircuitBreaker:
    """Process-wide singleton so all callers share breaker state."""
    if name not in _REGISTRY:
        _REGISTRY[name] = CircuitBreaker(name=name, **kwargs)
    return _REGISTRY[name]


def reset_breaker(name: str = "llm") -> None:
    """Test helper — wipe a breaker's state."""
    _REGISTRY.pop(name, None)

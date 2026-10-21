"""Sliding-window rate limiter keyed by tenant; in-process, clock-injected."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta


class SlidingWindowLimiter:
    def __init__(self, window: timedelta = timedelta(minutes=1)) -> None:
        self.window = window
        self._events: dict[str, deque[datetime]] = {}

    def check(self, key: str, limit: int, now: datetime, cost: int = 1) -> tuple[bool, int]:
        """Returns (allowed, remaining). Consumes `cost` when allowed."""
        q = self._events.setdefault(key, deque())
        cutoff = now - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) + cost > limit:
            return False, max(0, limit - len(q))
        q.extend([now] * cost)
        return True, limit - len(q)

    def retry_after(self, key: str, now: datetime) -> int:
        q = self._events.get(key)
        if not q:
            return 0
        return max(1, int((q[0] + self.window - now).total_seconds()))

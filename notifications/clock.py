"""Injectable clock.

Every service function receives `now` from a `Clock` instead of calling `datetime.now()`,
so a billing run can be executed for any `as_of` and tests can walk through months of
billing in milliseconds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime; naive values are assumed to be UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return utcnow()


class FixedClock:
    """A clock that only moves when told to. Used by tests and the simulator."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> datetime:
        self._now = ensure_utc(value)
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now

"""Retry schedule for failed deliveries."""

from __future__ import annotations

from datetime import datetime, timedelta


def parse_schedule(raw: str) -> tuple[int, ...]:
    values = tuple(int(part.strip()) for part in raw.split(",") if part.strip())
    if not values or any(v <= 0 for v in values):
        raise ValueError("retry schedule must be positive seconds")
    return values


def next_attempt_at(now: datetime, failed_attempts: int, schedule: tuple[int, ...]) -> datetime:
    """Delay after the n-th failure; the last schedule entry repeats."""
    if failed_attempts < 1:
        raise ValueError("failed_attempts must be >= 1")
    delay = schedule[min(failed_attempts, len(schedule)) - 1]
    return now + timedelta(seconds=delay)

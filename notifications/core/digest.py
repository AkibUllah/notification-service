"""Digest window keys and flush times."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from notifications.core.channels import DigestMode

DAILY_FLUSH_HOUR = 9  # local time


def window_key(now: datetime, mode: DigestMode, tz: str) -> str:
    local = now.astimezone(ZoneInfo(tz))
    if mode is DigestMode.HOURLY:
        return local.strftime("%Y-%m-%dT%H")
    if mode is DigestMode.DAILY:
        # Anything after today's flush hour belongs to tomorrow's digest.
        day = local.date() if local.hour < DAILY_FLUSH_HOUR else local.date() + timedelta(days=1)
        return day.isoformat()
    raise ValueError("no digest window for mode none")


def flush_at(now: datetime, mode: DigestMode, tz: str) -> datetime:
    """When the window that `now` falls in gets flushed, as an aware UTC datetime."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    if mode is DigestMode.HOURLY:
        local_flush = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    elif mode is DigestMode.DAILY:
        local_flush = local.replace(hour=DAILY_FLUSH_HOUR, minute=0, second=0, microsecond=0)
        if local_flush <= local:
            local_flush += timedelta(days=1)
    else:
        raise ValueError("no digest window for mode none")
    return local_flush.astimezone(now.tzinfo)

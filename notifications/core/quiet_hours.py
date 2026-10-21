"""Quiet hours in the user's own timezone (Asia/Dhaka by default)."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def in_quiet_hours(now: datetime, start: str | None, end: str | None, tz: str) -> bool:
    """True when the local wall-clock time is inside [start, end). Windows may cross midnight."""
    if not start or not end:
        return False
    local = now.astimezone(ZoneInfo(tz)).time().replace(tzinfo=None)
    s, e = parse_hhmm(start), parse_hhmm(end)
    if s == e:
        return False
    if s < e:
        return s <= local < e
    return local >= s or local < e  # overnight, e.g. 22:00 -> 08:00


def quiet_hours_end(now: datetime, start: str | None, end: str | None, tz: str) -> datetime:
    """The next instant the quiet window ends, in UTC. Only meaningful when in_quiet_hours()."""
    zone = ZoneInfo(tz)
    local_now = now.astimezone(zone)
    e = parse_hhmm(end or "00:00")
    candidate = local_now.replace(hour=e.hour, minute=e.minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(now.tzinfo)

from __future__ import annotations

from enum import Enum


class Channel(str, Enum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    IN_APP = "in_app"
    WEBHOOK = "webhook"


class Priority(str, Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"high": 0, "normal": 1, "low": 2}[self.value]


class DigestMode(str, Enum):
    NONE = "none"
    HOURLY = "hourly"
    DAILY = "daily"


EXTERNAL_CHANNELS = frozenset({Channel.EMAIL, Channel.SMS, Channel.PUSH})
"""Channels that interrupt the user; quiet hours and digests apply to these only."""

DEFAULT_CHANNELS = (Channel.IN_APP, Channel.EMAIL)
LOCALES = ("en", "bn")

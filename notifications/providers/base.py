from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from notifications.core.channels import Channel


@dataclass(frozen=True)
class Message:
    channel: Channel
    to: str
    body: str
    subject: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    locale: str = "en"
    notification_id: str | None = None
    delivery_id: str | None = None


@dataclass(frozen=True)
class ProviderResult:
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None
    retryable: bool = True


class ProviderNotConfigured(RuntimeError):
    pass


@runtime_checkable
class Provider(Protocol):
    name: str
    channel: Channel

    async def send(self, message: Message) -> ProviderResult: ...

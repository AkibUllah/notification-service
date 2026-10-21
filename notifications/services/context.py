from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from notifications.clock import Clock, SystemClock
from notifications.config import Settings
from notifications.core.channels import Channel
from notifications.core.ratelimit import SlidingWindowLimiter
from notifications.providers.chain import ProviderChain
from notifications.providers.registry import build_chains
from notifications.services.bus import InboxBus


@dataclass
class AppContext:
    settings: Settings
    chains: dict[Channel, ProviderChain]
    clock: Clock = field(default_factory=SystemClock)
    bus: InboxBus = field(default_factory=InboxBus)
    limiter: SlidingWindowLimiter = field(default_factory=SlidingWindowLimiter)
    http: httpx.AsyncClient = field(default_factory=lambda: httpx.AsyncClient(timeout=5))

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        chains: dict[Channel, ProviderChain] | None = None,
    ) -> AppContext:
        clock = clock or SystemClock()
        http = httpx.AsyncClient(timeout=settings.webhook_timeout_seconds, transport=transport)
        return cls(
            settings=settings,
            chains=chains or build_chains(settings, http, clock),
            clock=clock,
            bus=InboxBus(),
            limiter=SlidingWindowLimiter(),
            http=http,
        )

"""Fallback chain with per-provider health tracking (a small circuit breaker)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from notifications.clock import Clock, SystemClock
from notifications.core.channels import Channel
from notifications.providers.base import Message, Provider, ProviderNotConfigured, ProviderResult

log = logging.getLogger(__name__)


@dataclass
class Health:
    consecutive_failures: int = 0
    open_until: datetime | None = None
    sent: int = 0
    failed: int = 0
    last_error: str | None = None
    last_success_at: datetime | None = None

    def snapshot(self) -> dict:
        return {
            "healthy": self.open_until is None,
            "consecutive_failures": self.consecutive_failures,
            "open_until": self.open_until.isoformat() if self.open_until else None,
            "sent": self.sent,
            "failed": self.failed,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
        }


@dataclass
class ChainResult:
    result: ProviderResult
    provider: str | None
    attempts: list[tuple[str, str]] = field(default_factory=list)  # (provider, error)


class ProviderChain:
    def __init__(
        self,
        channel: Channel,
        providers: list[Provider],
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60,
        clock: Clock | None = None,
    ) -> None:
        if any(p.channel != channel for p in providers):
            raise ValueError("all providers in a chain must serve the chain's channel")
        self.channel = channel
        self.providers = providers
        self.failure_threshold = failure_threshold
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self.clock = clock or SystemClock()
        self.health: dict[str, Health] = {p.name: Health() for p in providers}

    def is_available(self, provider: Provider) -> bool:
        h = self.health[provider.name]
        if h.open_until is None:
            return True
        if self.clock.now() >= h.open_until:
            h.open_until = None  # half-open: let the next send probe it
            return True
        return False

    def _record(self, provider: Provider, result: ProviderResult) -> None:
        h = self.health[provider.name]
        if result.ok:
            h.consecutive_failures = 0
            h.open_until = None
            h.sent += 1
            h.last_success_at = self.clock.now()
            return
        h.failed += 1
        h.consecutive_failures += 1
        h.last_error = result.error
        if h.consecutive_failures >= self.failure_threshold:
            h.open_until = self.clock.now() + self.cooldown
            log.warning(
                "provider %s marked unhealthy until %s", provider.name, h.open_until.isoformat()
            )

    async def send(self, message: Message) -> ChainResult:
        """Try available providers in order; fall back on failure. If every provider is in
        cooldown, try them anyway rather than dropping the message."""
        candidates = [p for p in self.providers if self.is_available(p)] or list(self.providers)
        attempts: list[tuple[str, str]] = []
        last: ProviderResult | None = None
        for provider in candidates:
            try:
                result = await provider.send(message)
            except ProviderNotConfigured as exc:
                result = ProviderResult(False, error=f"not configured: {exc}", retryable=True)
            except Exception as exc:  # a provider bug must not take the worker down
                log.exception("provider %s raised", provider.name)
                result = ProviderResult(False, error=f"{type(exc).__name__}: {exc}", retryable=True)
            self._record(provider, result)
            if result.ok:
                return ChainResult(result, provider.name, attempts)
            attempts.append((provider.name, result.error or "unknown error"))
            last = result
            if not result.retryable:
                break  # e.g. invalid address: no other provider will do better
        error = "; ".join(f"{name}: {err}" for name, err in attempts) or "no providers configured"
        retryable = last.retryable if last else False
        return ChainResult(ProviderResult(False, error=error, retryable=retryable), None, attempts)

    def snapshot(self) -> dict:
        return {
            "channel": self.channel.value,
            "providers": [
                {"name": p.name, **self.health[p.name].snapshot()} for p in self.providers
            ],
        }

"""Build provider chains from settings."""

from __future__ import annotations

import httpx

from notifications.clock import Clock
from notifications.config import Settings
from notifications.core.channels import Channel
from notifications.providers.base import Provider
from notifications.providers.chain import ProviderChain
from notifications.providers.fakes import ConsoleEmailProvider, ConsoleSMSProvider, FilePushProvider
from notifications.providers.http import BdSmsAggregatorProvider, FcmProvider, WebhookProvider
from notifications.providers.smtp import SmtpProvider


def _provider(name: str, channel: Channel, settings: Settings, http: httpx.AsyncClient) -> Provider:
    table = {
        (Channel.EMAIL, "console"): lambda: ConsoleEmailProvider(),
        (Channel.EMAIL, "smtp"): lambda: SmtpProvider(
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_username,
            settings.smtp_password,
            settings.smtp_from,
        ),
        (Channel.SMS, "console"): lambda: ConsoleSMSProvider(),
        (Channel.SMS, "bd_sms"): lambda: BdSmsAggregatorProvider(
            http, settings.bd_sms_api_url, settings.bd_sms_api_key, settings.bd_sms_sender_id
        ),
        (Channel.PUSH, "file"): lambda: FilePushProvider(settings.push_file_path),
        (Channel.PUSH, "fcm"): lambda: FcmProvider(
            http, settings.fcm_project_id, settings.fcm_access_token
        ),
    }
    try:
        return table[(channel, name)]()
    except KeyError as exc:
        raise ValueError(f"unknown provider {name!r} for channel {channel.value}") from exc


def build_chains(
    settings: Settings, http: httpx.AsyncClient, clock: Clock
) -> dict[Channel, ProviderChain]:
    chains: dict[Channel, ProviderChain] = {}
    for channel in (Channel.EMAIL, Channel.SMS, Channel.PUSH):
        providers = [
            _provider(name, channel, settings, http)
            for name in settings.providers_for(channel.value)
        ]
        chains[channel] = ProviderChain(
            channel,
            providers,
            failure_threshold=settings.provider_failure_threshold,
            cooldown_seconds=settings.provider_cooldown_seconds,
            clock=clock,
        )
    chains[Channel.WEBHOOK] = ProviderChain(
        Channel.WEBHOOK,
        [WebhookProvider(http)],
        failure_threshold=settings.provider_failure_threshold,
        cooldown_seconds=settings.provider_cooldown_seconds,
        clock=clock,
    )
    return chains

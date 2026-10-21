import json
from datetime import timedelta

import httpx
import pytest

from notifications.clock import FixedClock
from notifications.core.channels import Channel
from notifications.providers.base import Message, ProviderNotConfigured
from notifications.providers.chain import ProviderChain
from notifications.providers.fakes import (
    ConsoleEmailProvider,
    ConsoleSMSProvider,
    FakeProvider,
    FilePushProvider,
)
from notifications.providers.http import BdSmsAggregatorProvider, FcmProvider, WebhookProvider
from notifications.providers.registry import build_chains
from notifications.providers.smtp import SmtpProvider
from tests.conftest import T0

MSG = Message(Channel.EMAIL, "akib@example.com", "body", subject="subj", data={"k": "v"})


async def test_console_and_file_providers(tmp_path):
    email, sms = ConsoleEmailProvider(), ConsoleSMSProvider()
    assert (await email.send(MSG)).ok and email.sent == [MSG]
    assert (
        await sms.send(Message(Channel.SMS, "+8801711000000", "hi"))
    ).provider_message_id == "console-sms-1"
    push = FilePushProvider(str(tmp_path / "out" / "push.jsonl"))
    result = await push.send(Message(Channel.PUSH, "tok", "body", subject="title", data={"a": 1}))
    assert result.ok
    line = json.loads((tmp_path / "out" / "push.jsonl").read_text().splitlines()[0])
    assert line["token"] == "tok" and line["title"] == "title" and line["data"] == {"a": 1}


async def test_chain_falls_back_to_next_provider():
    primary = FakeProvider(Channel.EMAIL, "primary", always_fail=True)
    backup = FakeProvider(Channel.EMAIL, "backup")
    chain = ProviderChain(
        Channel.EMAIL, [primary, backup], failure_threshold=3, clock=FixedClock(T0)
    )
    outcome = await chain.send(MSG)
    assert outcome.result.ok and outcome.provider == "backup"
    assert outcome.attempts == [("primary", "primary unavailable")]
    assert chain.health["primary"].failed == 1 and chain.health["backup"].sent == 1


async def test_chain_opens_circuit_and_recovers_after_cooldown():
    clock = FixedClock(T0)
    primary = FakeProvider(Channel.EMAIL, "primary", fail_times=2)
    backup = FakeProvider(Channel.EMAIL, "backup")
    chain = ProviderChain(
        Channel.EMAIL, [primary, backup], failure_threshold=2, cooldown_seconds=60, clock=clock
    )
    await chain.send(MSG)
    await chain.send(MSG)
    assert not chain.is_available(primary) and chain.snapshot()["providers"][0]["healthy"] is False
    await chain.send(MSG)
    assert primary.calls == 2  # skipped while open
    clock.advance(seconds=61)
    outcome = await chain.send(MSG)
    assert outcome.provider == "primary" and chain.is_available(primary)
    assert chain.health["primary"].consecutive_failures == 0


async def test_chain_tries_unhealthy_providers_when_nothing_else_is_left():
    clock = FixedClock(T0)
    only = FakeProvider(Channel.SMS, "only", fail_times=2)
    chain = ProviderChain(
        Channel.SMS, [only], failure_threshold=1, cooldown_seconds=600, clock=clock
    )
    assert not (await chain.send(Message(Channel.SMS, "+880", "x"))).result.ok
    assert not chain.is_available(only)
    assert not (await chain.send(Message(Channel.SMS, "+880", "x"))).result.ok
    assert (await chain.send(Message(Channel.SMS, "+880", "x"))).result.ok


async def test_chain_stops_on_non_retryable_failure():
    primary = FakeProvider(Channel.PUSH, "primary", always_fail=True, retryable=False)
    backup = FakeProvider(Channel.PUSH, "backup")
    chain = ProviderChain(Channel.PUSH, [primary, backup])
    outcome = await chain.send(Message(Channel.PUSH, "tok", "x"))
    assert not outcome.result.ok and not outcome.result.retryable and backup.calls == 0
    with pytest.raises(ValueError):
        ProviderChain(Channel.EMAIL, [primary])


async def test_stubs_require_configuration_and_chain_reports_it():
    with pytest.raises(ProviderNotConfigured):
        await SmtpProvider(None, 587, None, None, "x@y").send(MSG)
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(ProviderNotConfigured):
        await BdSmsAggregatorProvider(http, None, None, None).send(
            Message(Channel.SMS, "+880", "x")
        )
    with pytest.raises(ProviderNotConfigured):
        await FcmProvider(http, None, None).send(Message(Channel.PUSH, "tok", "x"))
    chain = ProviderChain(Channel.PUSH, [FcmProvider(http, None, None)])
    outcome = await chain.send(Message(Channel.PUSH, "tok", "x"))
    assert not outcome.result.ok and "not configured" in outcome.result.error


async def test_bd_sms_and_fcm_request_shapes():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request
        if "fcm" in request.url.host:
            if "dead-token" in request.read().decode():
                return httpx.Response(404, json={"error": {"status": "UNREGISTERED"}})
            return httpx.Response(200, json={"name": "projects/p/messages/1"})
        return httpx.Response(200, json={"response_code": 202, "message_id": 991})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sms = BdSmsAggregatorProvider(
        http, "https://sms.aggregator.example/smsapi", "key", "DHAKAGROCERS"
    )
    result = await sms.send(Message(Channel.SMS, "+8801711000000", "অর্ডার পাঠানো হয়েছে"))
    assert result.ok and result.provider_message_id == "991"
    body = seen["sms.aggregator.example"].read().decode()
    assert "senderid=DHAKAGROCERS" in body and "type=unicode" in body
    fcm = FcmProvider(http, "proj", "token")
    ok = await fcm.send(Message(Channel.PUSH, "good", "b", subject="t", data={"n": 1}))
    assert ok.ok and ok.provider_message_id == "projects/p/messages/1"
    assert seen["fcm.googleapis.com"].headers["Authorization"] == "Bearer token"
    dead = await fcm.send(Message(Channel.PUSH, "dead-token", "b"))
    assert not dead.ok and not dead.retryable


async def test_webhook_provider_and_smtp_message_build():
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        return httpx.Response(500 if payload["body"] == "boom" else 200)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    hook = WebhookProvider(http)
    assert (await hook.send(Message(Channel.WEBHOOK, "https://x.example/h", "ok"))).ok
    failed = await hook.send(Message(Channel.WEBHOOK, "https://x.example/h", "boom"))
    assert not failed.ok and failed.retryable
    email = SmtpProvider("smtp.example", 587, None, None, "no-reply@example.com").build(
        Message(Channel.EMAIL, "a@b.c", "<p>hi</p>", subject="S")
    )
    assert email["Subject"] == "S" and email.get_body(("html",)) is not None


def test_build_chains_from_settings(settings):
    http = httpx.AsyncClient()
    chains = build_chains(settings, http, FixedClock(T0))
    assert [p.name for p in chains[Channel.EMAIL].providers] == ["console_email"]
    assert [p.name for p in chains[Channel.PUSH].providers] == ["file_push"]
    assert chains[Channel.WEBHOOK].providers[0].name == "http_webhook"
    settings.sms_providers = "console,bd_sms"
    assert [
        p.name for p in build_chains(settings, http, FixedClock(T0))[Channel.SMS].providers
    ] == ["console_sms", "bd_sms"]
    settings.sms_providers = "pigeon"
    with pytest.raises(ValueError):
        build_chains(settings, http, FixedClock(T0))
    assert chains[Channel.EMAIL].cooldown == timedelta(seconds=60)

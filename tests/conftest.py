from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from notifications.clock import FixedClock
from notifications.config import Settings
from notifications.core.channels import Channel
from notifications.main import create_app
from notifications.providers.chain import ProviderChain
from notifications.providers.fakes import FakeProvider
from notifications.providers.http import WebhookProvider
from notifications.services import templates, tenants

ADMIN = {"X-API-Key": "test-admin-key"}
T0 = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)  # 12:00 in Dhaka (UTC+6)


@dataclass
class FakeReceiver:
    requests: list[httpx.Request] = field(default_factory=list)
    status: int = 200

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={"ok": True})


@dataclass
class Fakes:
    email: FakeProvider
    email_backup: FakeProvider
    sms: FakeProvider
    push: FakeProvider


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite://",
        admin_api_key="test-admin-key",
        worker_enabled=False,
        retry_schedule_seconds="30,120,600",
        max_attempts=3,
        push_file_path=str(tmp_path / "push.jsonl"),
        provider_failure_threshold=2,
        provider_cooldown_seconds=60,
        public_base_url="https://notify.example.com",
        log_level="WARNING",
    )


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def receiver() -> FakeReceiver:
    return FakeReceiver()


@pytest.fixture
def fakes() -> Fakes:
    return Fakes(
        email=FakeProvider(Channel.EMAIL, "email_primary"),
        email_backup=FakeProvider(Channel.EMAIL, "email_backup"),
        sms=FakeProvider(Channel.SMS, "sms_primary"),
        push=FakeProvider(Channel.PUSH, "push_primary"),
    )


@pytest.fixture
def chains(fakes, clock, settings, receiver):
    http = httpx.AsyncClient(transport=httpx.MockTransport(receiver.handler))
    kw = dict(
        failure_threshold=settings.provider_failure_threshold,
        cooldown_seconds=settings.provider_cooldown_seconds,
        clock=clock,
    )
    return {
        Channel.EMAIL: ProviderChain(Channel.EMAIL, [fakes.email, fakes.email_backup], **kw),
        Channel.SMS: ProviderChain(Channel.SMS, [fakes.sms], **kw),
        Channel.PUSH: ProviderChain(Channel.PUSH, [fakes.push], **kw),
        Channel.WEBHOOK: ProviderChain(Channel.WEBHOOK, [WebhookProvider(http)], **kw),
    }


@pytest.fixture
def app(settings, clock, chains, receiver):
    return create_app(
        settings, clock=clock, transport=httpx.MockTransport(receiver.handler), chains=chains
    )


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def ctx(app):
    return app.state.ctx


@pytest.fixture
def session(client, app):
    s = app.state.session_factory()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture
def tenant(session):
    t, key = tenants.create_tenant(
        session,
        name="Dhaka Grocers",
        now=T0,
        rate_limit_per_minute=100,
        webhook_url="https://shop.example.com/notify-status",
    )
    t.plaintext_key = key  # test convenience
    return t


@pytest.fixture
def user(session, tenant):
    return tenants.upsert_user(
        session,
        tenant,
        external_id="u-42",
        now=T0,
        default_locale="en",
        default_timezone="Asia/Dhaka",
        name="Akib",
        email="akib@example.com",
        phone="+8801711000000",
        push_token="fcm-token-1",
        webhook_url="https://shop.example.com/user-hook",
    )


@pytest.fixture
def order_templates(session, tenant):
    """`order-shipped` for every channel in en, plus a Bangla email variant."""
    made = {}
    made["email"] = templates.create_template(
        session,
        tenant,
        key="order-shipped",
        channel="email",
        locale="en",
        subject="Order {{ order_id }} shipped",
        body=(
            "Hi {{ user.name }}, order {{ order_id }} ({{ amount }}) is on its way.\n"
            "{{ unsubscribe_url }}"
        ),
        variables=["order_id", "amount"],
        now=T0,
    )
    made["email_bn"] = templates.create_template(
        session,
        tenant,
        key="order-shipped",
        channel="email",
        locale="bn",
        subject="অর্ডার {{ order_id }} পাঠানো হয়েছে",
        body="{{ user.name }}, আপনার অর্ডার {{ order_id }} ({{ amount }}) পথে আছে।",
        variables=["order_id", "amount"],
        now=T0,
    )
    made["sms"] = templates.create_template(
        session,
        tenant,
        key="order-shipped",
        channel="sms",
        locale="en",
        body="Order {{ order_id }} shipped. {{ amount }}",
        variables=["order_id", "amount"],
        now=T0,
    )
    made["push"] = templates.create_template(
        session,
        tenant,
        key="order-shipped",
        channel="push",
        locale="en",
        subject="Shipped!",
        body="Order {{ order_id }} is on its way",
        variables=["order_id", "amount"],
        now=T0,
    )
    made["in_app"] = templates.create_template(
        session,
        tenant,
        key="order-shipped",
        channel="in_app",
        locale="en",
        subject="Order {{ order_id }} shipped",
        body="{{ amount }} · tap to track",
        variables=["order_id", "amount"],
        now=T0,
    )
    made["webhook"] = templates.create_template(
        session,
        tenant,
        key="order-shipped",
        channel="webhook",
        locale="en",
        body='{"order_id": "{{ order_id }}", "amount": "{{ amount }}"}',
        variables=["order_id", "amount"],
        now=T0,
    )
    session.commit()
    return made


VARS = {"order_id": "ORD-4521", "amount": "৳1,500.00"}

import json
from datetime import timedelta

import pytest

from notifications.core.channels import Channel, Priority
from notifications.core.retry import next_attempt_at, parse_schedule
from notifications.core.signing import HEADER, verify
from notifications.services import inbox, notifications
from notifications.services.errors import DomainError, RateLimited
from notifications.services.worker import process_once, record_receipt, replay
from tests.conftest import T0, VARS


def enqueue(session, ctx, tenant, user, now=T0, **kw):
    kw.setdefault("category", "orders")
    kw.setdefault("template_key", "order-shipped")
    kw.setdefault("variables", VARS)
    return notifications.enqueue(session, ctx, tenant, user, now=now, **kw)[0]


def test_retry_schedule_helpers():
    schedule = parse_schedule("30,120,600")
    assert next_attempt_at(T0, 1, schedule) == T0 + timedelta(seconds=30)
    assert next_attempt_at(T0, 3, schedule) == T0 + timedelta(seconds=600)
    assert next_attempt_at(T0, 9, schedule) == T0 + timedelta(seconds=600)
    with pytest.raises(ValueError):
        parse_schedule("0,1")


async def test_sends_all_channels_and_records_everything(
    session, ctx, tenant, user, order_templates, fakes, receiver
):
    n = enqueue(
        session,
        ctx,
        tenant,
        user,
        channels=[Channel.EMAIL, Channel.SMS, Channel.PUSH, Channel.IN_APP, Channel.WEBHOOK],
    )
    assert n.status == "queued" and all(d.status == "pending" for d in n.deliveries)
    stats = await process_once(session, ctx, T0)
    assert stats.as_dict() == {
        "processed": 5,
        "sent": 5,
        "retried": 0,
        "dead": 0,
        "by_channel": {"email": 1, "sms": 1, "push": 1, "in_app": 1, "webhook": 1},
    }
    session.refresh(n)
    assert n.status == "sent"
    by_channel = {d.channel.value: d for d in n.deliveries}
    assert (
        by_channel["email"].provider == "email_primary"
        and by_channel["email"].provider_message_id == "email_primary-1"
    )
    assert (
        "unsubscribe/" in fakes.email.sent[0].body and fakes.email.sent[0].to == "akib@example.com"
    )
    assert fakes.sms.sent[0].body == "Order ORD-4521 shipped. ৳1,500.00"
    assert (
        fakes.push.sent[0].subject == "Shipped!"
        and fakes.push.sent[0].data["order_id"] == "ORD-4521"
    )
    assert by_channel["in_app"].provider == "inbox" and inbox.unread_count(session, user) == 1
    assert inbox.list_messages(session, user)[0].title == "Order ORD-4521 shipped"
    hook = [r for r in receiver.requests if r.url.path == "/user-hook"][0]
    assert json.loads(hook.content)["body"] == '{"order_id": "ORD-4521", "amount": "৳1,500.00"}'
    status_calls = [r for r in receiver.requests if r.url.path == "/notify-status"]
    assert len(status_calls) == 5
    verify(
        tenant.webhook_secret,
        status_calls[0].content,
        status_calls[0].headers[HEADER],
        now=int(T0.timestamp()),
    )
    assert json.loads(status_calls[0].content)["type"] == "delivery.sent"
    assert (await process_once(session, ctx, T0)).processed == 0  # nothing left to do


async def test_priority_order_and_batch_size(session, ctx, tenant, user, order_templates, fakes):
    low = enqueue(
        session,
        ctx,
        tenant,
        user,
        channels=[Channel.SMS],
        priority=Priority.LOW,
        variables={**VARS, "order_id": "LOW"},
    )
    high = enqueue(
        session,
        ctx,
        tenant,
        user,
        channels=[Channel.SMS],
        priority=Priority.HIGH,
        variables={**VARS, "order_id": "HIGH"},
        now=T0 + timedelta(seconds=1),
    )
    normal = enqueue(
        session,
        ctx,
        tenant,
        user,
        channels=[Channel.SMS],
        variables={**VARS, "order_id": "NORMAL"},
        now=T0 + timedelta(seconds=2),
    )
    stats = await process_once(session, ctx, T0 + timedelta(seconds=5), batch_size=2)
    assert stats.processed == 2
    assert [m.body.split()[1] for m in fakes.sms.sent] == ["HIGH", "NORMAL"]
    await process_once(session, ctx, T0 + timedelta(seconds=5))
    assert [m.body.split()[1] for m in fakes.sms.sent] == ["HIGH", "NORMAL", "LOW"]
    assert {n.status for n in (low, high, normal)} == {"sent"}


async def test_retries_with_backoff_then_dead_letter_then_replay(
    session, ctx, tenant, user, order_templates, fakes, receiver
):
    fakes.sms.always_fail = True
    n = enqueue(session, ctx, tenant, user, channels=[Channel.SMS])
    (d,) = n.deliveries
    stats = await process_once(session, ctx, T0)
    assert stats.retried == 1 and d.status == "pending" and d.attempts == 1
    assert (
        d.next_attempt_at == T0 + timedelta(seconds=30)
        and "sms_primary unavailable" in d.last_error
    )
    assert (await process_once(session, ctx, T0 + timedelta(seconds=29))).processed == 0
    await process_once(session, ctx, T0 + timedelta(seconds=30))
    assert d.attempts == 2 and d.next_attempt_at == T0 + timedelta(seconds=150)
    stats = await process_once(session, ctx, T0 + timedelta(seconds=150))
    assert stats.dead == 1 and d.status == "dead" and d.next_attempt_at is None
    session.refresh(n)
    assert n.status == "failed"
    dead_events = [
        json.loads(r.content) for r in receiver.requests if r.url.path == "/notify-status"
    ]
    assert dead_events[-1]["type"] == "delivery.dead" and dead_events[-1]["data"]["attempts"] == 3

    fakes.sms.always_fail = False
    replay(session, d, T0 + timedelta(hours=1))
    assert d.status == "pending" and d.attempts == 0 and n.status == "queued"
    await process_once(session, ctx, T0 + timedelta(hours=1))
    assert d.status == "sent"
    with pytest.raises(DomainError, match="not_replayable|only dead"):
        d.status = "pending"
        replay(session, d, T0)


async def test_fallback_provider_used_when_primary_fails(
    session, ctx, tenant, user, order_templates, fakes
):
    fakes.email.always_fail = True
    n = enqueue(session, ctx, tenant, user, channels=[Channel.EMAIL])
    await process_once(session, ctx, T0)
    assert n.deliveries[0].status == "sent" and n.deliveries[0].provider == "email_backup"


async def test_missing_template_is_rejected_at_enqueue(session, ctx, tenant, user, order_templates):
    with pytest.raises(Exception, match="template_not_found|no sms template"):
        enqueue(session, ctx, tenant, user, template_key="ghost", channels=[Channel.SMS])


async def test_render_error_in_worker_dead_letters_without_retry(
    session, ctx, tenant, user, order_templates
):
    n = enqueue(
        session, ctx, tenant, user, channels=[Channel.SMS], variables={"order_id": "x"}
    )  # missing amount
    stats = await process_once(session, ctx, T0)
    assert (
        stats.dead == 1
        and n.deliveries[0].attempts == 1
        and "missing variables" in n.deliveries[0].last_error
    )


def test_idempotency_and_rate_limit(session, ctx, tenant, user, order_templates):
    first = enqueue(session, ctx, tenant, user, idempotency_key="evt-1", channels=[Channel.SMS])
    again, created = notifications.enqueue(
        session,
        ctx,
        tenant,
        user,
        category="orders",
        template_key="order-shipped",
        variables=VARS,
        now=T0,
        idempotency_key="evt-1",
    )
    assert again.id == first.id and created is False
    tenant.rate_limit_per_minute = 3
    for _ in range(2):
        enqueue(session, ctx, tenant, user, channels=[Channel.SMS])
    with pytest.raises(RateLimited) as exc:
        enqueue(session, ctx, tenant, user, channels=[Channel.SMS])
    assert exc.value.retry_after == 60
    enqueue(session, ctx, tenant, user, channels=[Channel.SMS], now=T0 + timedelta(seconds=61))


async def test_quiet_hours_deferral_is_released_by_the_clock(
    session, ctx, tenant, user, order_templates, fakes
):
    user.quiet_hours_start, user.quiet_hours_end = "22:00", "08:00"
    night = T0.replace(hour=18)  # 00:00 Dhaka
    n = enqueue(session, ctx, tenant, user, channels=[Channel.SMS, Channel.IN_APP], now=night)
    sms = next(d for d in n.deliveries if d.channel is Channel.SMS)
    assert sms.skip_reason == "quiet_hours" and sms.next_attempt_at == night + timedelta(hours=8)
    stats = await process_once(session, ctx, night)
    assert stats.by_channel == {"in_app": 1}
    await process_once(session, ctx, night + timedelta(hours=8))
    assert sms.status == "sent"


async def test_receipts_update_delivery(session, ctx, tenant, user, order_templates, fakes):
    n = enqueue(session, ctx, tenant, user, channels=[Channel.EMAIL])
    await process_once(session, ctx, T0)
    d = record_receipt(session, "email_primary-1", "bounced", T0 + timedelta(minutes=5))
    assert d.id == n.deliveries[0].id and d.receipt_status == "bounced"

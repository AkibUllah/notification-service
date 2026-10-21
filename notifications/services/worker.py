"""The delivery worker: pick due deliveries by priority, render, send through the chain,
retry with backoff, dead-letter after MAX_ATTEMPTS, and report status to the tenant."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from notifications.core.channels import Channel
from notifications.core.retry import next_attempt_at
from notifications.models import Delivery, Notification, Tenant, User
from notifications.providers.base import Message
from notifications.services import inbox, status_webhooks
from notifications.services.context import AppContext
from notifications.services.errors import DomainError, NotFound
from notifications.services.notifications import render_for, summarize

log = logging.getLogger(__name__)


@dataclass
class RunStats:
    processed: int = 0
    sent: int = 0
    retried: int = 0
    dead: int = 0
    by_channel: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "processed": self.processed,
            "sent": self.sent,
            "retried": self.retried,
            "dead": self.dead,
            "by_channel": self.by_channel,
        }


def due_deliveries(session: Session, now: datetime, limit: int) -> list[Delivery]:
    return list(
        session.scalars(
            select(Delivery)
            .where(
                Delivery.status == "pending",
                Delivery.next_attempt_at.is_not(None),
                Delivery.next_attempt_at <= now,
            )
            .order_by(Delivery.priority_rank, Delivery.next_attempt_at, Delivery.id)
            .limit(limit)
        )
    )


async def deliver(session: Session, ctx: AppContext, delivery: Delivery, now: datetime) -> str:
    """Attempt one delivery. Returns the resulting status."""
    notification = session.get(Notification, delivery.notification_id)
    tenant = session.get(Tenant, delivery.tenant_id)
    user = session.get(User, delivery.user_id)
    channel = Channel(delivery.channel)
    delivery.attempts += 1
    delivery.updated_at = now
    try:
        if delivery.rendered_body is None:
            rendered = render_for(session, ctx, tenant, user, notification, channel, now)
            delivery.rendered_subject, delivery.rendered_body = rendered.subject, rendered.body
    except (DomainError, NotFound) as exc:
        return _fail(delivery, f"render: {exc.message}", retryable=False, ctx=ctx, now=now)

    if channel is Channel.IN_APP:
        inbox.add_message(
            session,
            ctx.bus,
            user,
            notification_id=notification.id,
            category=notification.category,
            title=delivery.rendered_subject,
            body=delivery.rendered_body,
            data=dict(notification.variables),
            now=now,
        )
        delivery.provider = "inbox"
        return _sent(delivery, provider_message_id=None, now=now)

    message = Message(
        channel=channel,
        to=user.address_for(channel) or "",
        subject=delivery.rendered_subject,
        body=delivery.rendered_body,
        data={
            "notification_id": notification.id,
            "category": notification.category,
            **{
                k: v
                for k, v in notification.variables.items()
                if isinstance(v, str | int | float | bool)
            },
        },
        locale=user.locale,
        notification_id=notification.id,
        delivery_id=delivery.id,
    )
    chain = ctx.chains.get(channel)
    if chain is None or not chain.providers:
        return _fail(
            delivery,
            f"no providers configured for {channel.value}",
            retryable=False,
            ctx=ctx,
            now=now,
        )
    outcome = await chain.send(message)
    if outcome.result.ok:
        delivery.provider = outcome.provider
        return _sent(delivery, outcome.result.provider_message_id, now)
    return _fail(
        delivery,
        outcome.result.error or "send failed",
        retryable=outcome.result.retryable,
        ctx=ctx,
        now=now,
    )


def _sent(delivery: Delivery, provider_message_id: str | None, now: datetime) -> str:
    delivery.status = "sent"
    delivery.provider_message_id = provider_message_id
    delivery.sent_at = now
    delivery.next_attempt_at = None
    delivery.last_error = None
    return delivery.status


def _fail(
    delivery: Delivery, error: str, *, retryable: bool, ctx: AppContext, now: datetime
) -> str:
    delivery.last_error = error[:2000]
    if retryable and delivery.attempts < ctx.settings.max_attempts:
        delivery.status = "pending"
        delivery.next_attempt_at = next_attempt_at(
            now, delivery.attempts, ctx.settings.retry_schedule
        )
        return "retry"
    delivery.status = "dead"
    delivery.next_attempt_at = None
    return delivery.status


async def process_once(
    session: Session, ctx: AppContext, now: datetime, batch_size: int | None = None
) -> RunStats:
    stats = RunStats()
    for delivery in due_deliveries(session, now, batch_size or ctx.settings.worker_batch_size):
        outcome = await deliver(session, ctx, delivery, now)
        stats.processed += 1
        stats.by_channel[delivery.channel.value] = (
            stats.by_channel.get(delivery.channel.value, 0) + 1
        )
        notification = session.get(Notification, delivery.notification_id)
        notification.status = summarize(notification)
        notification.updated_at = now
        session.commit()
        if outcome == "sent":
            stats.sent += 1
        elif outcome == "retry":
            stats.retried += 1
        else:
            stats.dead += 1
        if outcome in ("sent", "dead"):
            tenant = session.get(Tenant, delivery.tenant_id)
            await status_webhooks.notify(ctx, tenant, f"delivery.{outcome}", delivery, now)
    return stats


def replay(session: Session, delivery: Delivery, now: datetime) -> Delivery:
    if delivery.status not in ("dead", "sent", "skipped"):
        raise DomainError(
            f"delivery is {delivery.status}; only dead, sent or skipped deliveries can be replayed",
            code="not_replayable",
        )
    delivery.status = "pending"
    delivery.skip_reason = None
    delivery.next_attempt_at = now
    delivery.attempts = 0
    delivery.updated_at = now
    notification = session.get(Notification, delivery.notification_id)
    notification.status = summarize(notification)
    session.flush()
    return delivery


def record_receipt(
    session: Session, provider_message_id: str, status: str, now: datetime
) -> Delivery:
    delivery = session.scalar(
        select(Delivery).where(Delivery.provider_message_id == provider_message_id)
    )
    if delivery is None:
        raise NotFound(f"no delivery with provider message id {provider_message_id!r}")
    delivery.receipt_status = status
    delivery.updated_at = now
    session.flush()
    return delivery

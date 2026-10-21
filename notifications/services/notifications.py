"""Accepting notifications: idempotency, rate limits, preference resolution, queueing."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from notifications.core.channels import DEFAULT_CHANNELS, Channel, Priority
from notifications.core.digest import flush_at, window_key
from notifications.models import Delivery, DigestEntry, Notification, Tenant, User
from notifications.services import preferences, templates
from notifications.services.context import AppContext
from notifications.services.errors import NotFound, RateLimited


def unsubscribe_url(
    ctx: AppContext, session: Session, user: User, category: str, channel: Channel, now: datetime
) -> str | None:
    if channel not in (Channel.EMAIL, Channel.SMS, Channel.PUSH):
        return None
    token = preferences.issue_unsubscribe_token(
        session, user, category=category, channel=channel, now=now
    )
    return f"{ctx.settings.public_base_url.rstrip('/')}/v1/unsubscribe/{token.token}"


def render_for(
    session: Session,
    ctx: AppContext,
    tenant: Tenant,
    user: User,
    notification: Notification,
    channel: Channel,
    now: datetime,
    *,
    with_unsubscribe: bool = True,
) -> templates.Rendered:
    template = templates.resolve(
        session,
        tenant,
        notification.template_key,
        channel,
        user.locale,
        default_locale=ctx.settings.default_locale,
    )
    url = (
        unsubscribe_url(ctx, session, user, notification.category, channel, now)
        if with_unsubscribe
        else None
    )
    return templates.render(
        template, notification.variables, user=user, now=now, unsubscribe_url=url
    )


def enqueue(
    session: Session,
    ctx: AppContext,
    tenant: Tenant,
    user: User,
    *,
    category: str,
    template_key: str,
    variables: dict[str, Any],
    now: datetime,
    channels: list[Channel | str] | None = None,
    priority: Priority | str = Priority.NORMAL,
    idempotency_key: str | None = None,
    metadata: dict[str, Any] | None = None,
    check_rate_limit: bool = True,
) -> tuple[Notification, bool]:
    """Create a notification and its per-channel deliveries. Returns (notification, created).

    Replaying an idempotency key returns the stored notification unchanged. Templates are
    resolved up front so a missing template fails the request instead of the worker.
    """
    priority = Priority(priority)
    requested = [Channel(c) for c in (channels or DEFAULT_CHANNELS)]
    if idempotency_key:
        existing = session.scalar(
            select(Notification).where(
                Notification.tenant_id == tenant.id, Notification.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing, False
    if check_rate_limit:
        allowed, _ = ctx.limiter.check(tenant.id, tenant.rate_limit_per_minute, now)
        if not allowed:
            raise RateLimited(
                "tenant rate limit exceeded", retry_after=ctx.limiter.retry_after(tenant.id, now)
            )

    notification = Notification(
        tenant_id=tenant.id,
        user_id=user.id,
        category=category,
        template_key=template_key,
        variables=variables,
        priority=priority,
        channels_requested=[c.value for c in requested],
        idempotency_key=idempotency_key,
        metadata_=metadata or {},
        created_at=now,
        updated_at=now,
    )
    session.add(notification)
    session.flush()

    resolution = preferences.resolve(user, category, requested, priority, now)
    for decision in resolution.decisions:
        delivery = Delivery(
            notification_id=notification.id,
            tenant_id=tenant.id,
            user_id=user.id,
            channel=decision.channel,
            priority_rank=priority.rank,
            created_at=now,
            updated_at=now,
        )
        if decision.action == "skip":
            delivery.status, delivery.skip_reason = "skipped", decision.reason
        elif decision.action == "defer":
            delivery.status, delivery.next_attempt_at, delivery.skip_reason = (
                "pending",
                decision.deliver_at,
                "quiet_hours",
            )
        elif decision.action == "digest":
            rendered = render_for(
                session,
                ctx,
                tenant,
                user,
                notification,
                decision.channel,
                now,
                with_unsubscribe=False,
            )
            delivery.status, delivery.skip_reason = "digested", decision.reason
            delivery.rendered_subject, delivery.rendered_body = rendered.subject, rendered.body
            session.add(
                DigestEntry(
                    user_id=user.id,
                    tenant_id=tenant.id,
                    notification_id=notification.id,
                    category=category,
                    channel=decision.channel,
                    window_key=window_key(now, decision.digest, user.timezone),
                    flush_at=flush_at(now, decision.digest, user.timezone),
                    subject=rendered.subject,
                    body=rendered.body,
                    created_at=now,
                )
            )
        else:
            delivery.status, delivery.next_attempt_at = "pending", now
        if delivery.status == "pending":
            # Resolve the template now so a 404 surfaces to the caller, not to the worker.
            templates.resolve(
                session,
                tenant,
                template_key,
                decision.channel,
                user.locale,
                default_locale=ctx.settings.default_locale,
            )
        notification.deliveries.append(delivery)
    notification.status = summarize(notification)
    session.flush()
    return notification, True


def summarize(notification: Notification) -> str:
    statuses = {d.status for d in notification.deliveries}
    if not statuses:
        return "suppressed"
    if statuses <= {"skipped"}:
        return "suppressed"
    if statuses <= {"skipped", "digested"}:
        return "digested"
    if "pending" in statuses:
        return "queued"
    if "dead" in statuses or "failed" in statuses:
        return "partially_sent" if "sent" in statuses else "failed"
    return "sent"


def get_notification(session: Session, tenant: Tenant, notification_id: str) -> Notification:
    notification = session.get(Notification, notification_id)
    if notification is None or notification.tenant_id != tenant.id:
        raise NotFound(f"notification {notification_id!r} not found")
    return notification


def list_notifications(
    session: Session,
    tenant: Tenant,
    *,
    user_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[Notification], int]:
    stmt = select(Notification).where(Notification.tenant_id == tenant.id)
    if user_id:
        stmt = stmt.where(Notification.user_id == user_id)
    if status:
        stmt = stmt.where(Notification.status == status)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.order_by(Notification.created_at.desc(), Notification.id).limit(limit).offset(offset)
    ).all()
    return list(rows), total


def list_deliveries(
    session: Session,
    tenant: Tenant,
    *,
    status: str | None = None,
    channel: Channel | None = None,
    limit: int = 100,
) -> list[Delivery]:
    stmt = select(Delivery).where(Delivery.tenant_id == tenant.id)
    if status:
        stmt = stmt.where(Delivery.status == status)
    if channel:
        stmt = stmt.where(Delivery.channel == channel)
    return list(
        session.scalars(stmt.order_by(Delivery.created_at.desc(), Delivery.id).limit(limit))
    )

"""Flush digest windows into one combined notification per (user, category, channel)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from notifications.core.channels import Channel, Priority
from notifications.models import Delivery, DigestEntry, Notification, Tenant, User
from notifications.services import templates
from notifications.services.context import AppContext
from notifications.services.errors import NotFound

DIGEST_TEMPLATE_KEY = "digest"

_FALLBACK_SUBJECT = {
    "en": "You have {count} new notifications",
    "bn": "আপনার {count}টি নতুন নোটিফিকেশন আছে",
}
_FALLBACK_INTRO = {"en": "While you were away:", "bn": "আপনি দূরে থাকাকালীন:"}


def _fallback_render(locale: str, items: list[dict[str, Any]]) -> tuple[str, str]:
    lang = locale if locale in _FALLBACK_SUBJECT else "en"
    lines = [_FALLBACK_INTRO[lang], ""]
    for item in items:
        title = f"{item['subject']}: " if item.get("subject") else ""
        lines.append(f"- {title}{item['body'].strip()}")
    return _FALLBACK_SUBJECT[lang].format(count=len(items)), "\n".join(lines)


def due_entries(session: Session, now: datetime) -> list[DigestEntry]:
    return list(
        session.scalars(
            select(DigestEntry)
            .where(DigestEntry.flushed_at.is_(None), DigestEntry.flush_at <= now)
            .order_by(
                DigestEntry.user_id, DigestEntry.category, DigestEntry.channel, DigestEntry.id
            )
        )
    )


def flush_due(session: Session, ctx: AppContext, now: datetime) -> list[Notification]:
    """Group due entries and queue one delivery per group. Uses the tenant's `digest` template
    for the channel/locale when one exists, otherwise a plain built-in list."""
    groups: dict[tuple[str, str, str, str], list[DigestEntry]] = {}
    for entry in due_entries(session, now):
        groups.setdefault(
            (entry.user_id, entry.tenant_id, entry.category, entry.channel.value), []
        ).append(entry)

    created: list[Notification] = []
    for (user_id, tenant_id, category, channel_value), entries in groups.items():
        user = session.get(User, user_id)
        tenant = session.get(Tenant, tenant_id)
        channel = Channel(channel_value)
        items = [
            {
                "subject": e.subject,
                "body": e.body,
                "notification_id": e.notification_id,
                "created_at": e.created_at.isoformat(),
            }
            for e in entries
        ]
        try:
            template = templates.resolve(
                session,
                tenant,
                DIGEST_TEMPLATE_KEY,
                channel,
                user.locale,
                default_locale=ctx.settings.default_locale,
            )
            rendered = templates.render(
                template,
                {"items": items, "count": len(items), "category": category},
                user=user,
                now=now,
                unsubscribe_url=None,
            )
            subject, body = rendered.subject, rendered.body
        except NotFound:
            subject, body = _fallback_render(user.locale, items)

        notification = Notification(
            tenant_id=tenant.id,
            user_id=user.id,
            category=category,
            template_key=DIGEST_TEMPLATE_KEY,
            variables={
                "count": len(items),
                "notification_ids": [e.notification_id for e in entries],
            },
            priority=Priority.LOW,
            channels_requested=[channel.value],
            status="queued",
            metadata_={"digest": True, "window_key": entries[0].window_key},
            created_at=now,
            updated_at=now,
        )
        session.add(notification)
        session.flush()
        notification.deliveries.append(
            Delivery(
                notification_id=notification.id,
                tenant_id=tenant.id,
                user_id=user.id,
                channel=channel,
                priority_rank=Priority.LOW.rank,
                status="pending",
                next_attempt_at=now,
                rendered_subject=subject,
                rendered_body=body,
                created_at=now,
                updated_at=now,
            )
        )
        for entry in entries:
            entry.flushed_at = now
            entry.digest_notification_id = notification.id
        created.append(notification)
    session.flush()
    return created


def pending_entries(session: Session, user: User) -> list[DigestEntry]:
    return list(
        session.scalars(
            select(DigestEntry)
            .where(DigestEntry.user_id == user.id, DigestEntry.flushed_at.is_(None))
            .order_by(DigestEntry.id)
        )
    )

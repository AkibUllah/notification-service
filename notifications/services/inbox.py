from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from notifications.models import InboxMessage, User
from notifications.services.bus import InboxBus


def message_payload(message: InboxMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "notification_id": message.notification_id,
        "category": message.category,
        "title": message.title,
        "body": message.body,
        "data": message.data,
        "read_at": message.read_at.isoformat() if message.read_at else None,
        "created_at": message.created_at.isoformat(),
    }


def add_message(
    session: Session,
    bus: InboxBus,
    user: User,
    *,
    notification_id: str | None,
    category: str,
    title: str | None,
    body: str,
    data: dict[str, Any],
    now: datetime,
) -> InboxMessage:
    message = InboxMessage(
        user_id=user.id,
        notification_id=notification_id,
        category=category,
        title=title,
        body=body,
        data=data,
        created_at=now,
    )
    session.add(message)
    session.flush()
    bus.publish(
        user.id,
        {
            "type": "message",
            "message": message_payload(message),
            "unread_count": unread_count(session, user),
        },
    )
    return message


def list_messages(
    session: Session,
    user: User,
    *,
    unread_only: bool = False,
    limit: int = 50,
    before: datetime | None = None,
) -> list[InboxMessage]:
    stmt = select(InboxMessage).where(InboxMessage.user_id == user.id)
    if unread_only:
        stmt = stmt.where(InboxMessage.read_at.is_(None))
    if before is not None:
        stmt = stmt.where(InboxMessage.created_at < before)
    return list(
        session.scalars(
            stmt.order_by(InboxMessage.created_at.desc(), InboxMessage.id.desc()).limit(limit)
        )
    )


def unread_count(session: Session, user: User) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(InboxMessage)
            .where(InboxMessage.user_id == user.id, InboxMessage.read_at.is_(None))
        )
        or 0
    )


def mark_read(
    session: Session, bus: InboxBus, user: User, *, ids: list[str] | None, now: datetime
) -> int:
    stmt = select(InboxMessage).where(
        InboxMessage.user_id == user.id, InboxMessage.read_at.is_(None)
    )
    if ids is not None:
        stmt = stmt.where(InboxMessage.id.in_(ids))
    count = 0
    for message in session.scalars(stmt):
        message.read_at = now
        count += 1
    session.flush()
    if count:
        bus.publish(
            user.id, {"type": "read", "count": count, "unread_count": unread_count(session, user)}
        )
    return count

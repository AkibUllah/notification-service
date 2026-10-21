from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from notifications.core.channels import Channel, DigestMode, Priority


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime; use aware UTC values")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=timezone.utc)


def enum_column(enum_cls, length: int) -> SAEnum:
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime, dict[str, Any]: JSON, list[Any]: JSON}


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("tnt"))
    name: Mapped[str] = mapped_column(String(200))
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    api_key_prefix: Mapped[str] = mapped_column(String(16))
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=600)
    webhook_url: Mapped[str | None] = mapped_column(String(2000))
    webhook_secret: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime]


class User(Base):
    """A recipient. `external_id` is the tenant's own user id."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "external_id", name="uq_user_tenant_external"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("usr"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(32))
    push_token: Mapped[str | None] = mapped_column(String(500))
    webhook_url: Mapped[str | None] = mapped_column(String(2000))
    locale: Mapped[str] = mapped_column(String(5), default="en")
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Dhaka")
    quiet_hours_start: Mapped[str | None] = mapped_column(String(5))  # "22:00" local
    quiet_hours_end: Mapped[str | None] = mapped_column(String(5))  # "08:00" local
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    preferences: Mapped[list[Preference]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    def address_for(self, channel: Channel) -> str | None:
        return {
            Channel.EMAIL: self.email,
            Channel.SMS: self.phone,
            Channel.PUSH: self.push_token,
            Channel.WEBHOOK: self.webhook_url,
            Channel.IN_APP: self.id,
        }[channel]


class Preference(Base):
    """Per-category preference: which channels are allowed and whether to batch into a digest."""

    __tablename__ = "preferences"
    __table_args__ = (UniqueConstraint("user_id", "category", name="uq_pref_user_category"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(60))  # "*" is the catch-all
    channels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {"email": false, ...}
    digest: Mapped[DigestMode] = mapped_column(enum_column(DigestMode, 10), default=DigestMode.NONE)
    updated_at: Mapped[datetime]

    user: Mapped[User] = relationship(back_populates="preferences")


class UnsubscribeToken(Base):
    __tablename__ = "unsubscribe_tokens"

    token: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: secrets.token_urlsafe(32)
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category: Mapped[str | None] = mapped_column(String(60))  # None = everything
    channel: Mapped[str | None] = mapped_column(String(10))  # None = all channels
    created_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


class Template(Base):
    __tablename__ = "templates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "key", "channel", "locale", "version", name="uq_template_version"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("tpl"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    key: Mapped[str] = mapped_column(String(100), index=True)
    channel: Mapped[Channel] = mapped_column(enum_column(Channel, 10))
    locale: Mapped[str] = mapped_column(String(5))
    version: Mapped[int] = mapped_column(Integer, default=1)
    subject: Mapped[str | None] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    variables: Mapped[list[Any]] = mapped_column(JSON, default=list)  # declared variable names
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime]


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_notification_idempotency"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ntf"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    category: Mapped[str] = mapped_column(String(60))
    template_key: Mapped[str] = mapped_column(String(100))
    variables: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    priority: Mapped[Priority] = mapped_column(enum_column(Priority, 10), default=Priority.NORMAL)
    channels_requested: Mapped[list[Any]] = mapped_column(JSON, default=list)
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    deliveries: Mapped[list[Delivery]] = relationship(
        back_populates="notification",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Delivery.id",
    )


class Delivery(Base):
    """One channel attempt-stream for a notification. The worker queue is `status='pending'`."""

    __tablename__ = "deliveries"
    __table_args__ = (Index("ix_deliveries_queue", "status", "priority_rank", "next_attempt_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("dlv"))
    notification_id: Mapped[str] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    channel: Mapped[Channel] = mapped_column(enum_column(Channel, 10))
    priority_rank: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(
        String(12), default="pending"
    )  # pending|sent|failed|dead|skipped|digested
    skip_reason: Mapped[str | None] = mapped_column(String(60))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None]
    provider: Mapped[str | None] = mapped_column(String(40))
    provider_message_id: Mapped[str | None] = mapped_column(String(200), index=True)
    rendered_subject: Mapped[str | None] = mapped_column(String(500))
    rendered_body: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    receipt_status: Mapped[str | None] = mapped_column(String(20))  # delivered|bounced|read|...
    sent_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    notification: Mapped[Notification] = relationship(back_populates="deliveries")


class InboxMessage(Base):
    __tablename__ = "inbox_messages"
    __table_args__ = (Index("ix_inbox_user_read_created", "user_id", "read_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("msg"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    notification_id: Mapped[str | None] = mapped_column(
        ForeignKey("notifications.id", ondelete="SET NULL")
    )
    category: Mapped[str] = mapped_column(String(60))
    title: Mapped[str | None] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    read_at: Mapped[datetime | None]
    created_at: Mapped[datetime]


class DigestEntry(Base):
    __tablename__ = "digest_entries"
    __table_args__ = (Index("ix_digest_pending", "flushed_at", "flush_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    notification_id: Mapped[str] = mapped_column(ForeignKey("notifications.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(60))
    channel: Mapped[Channel] = mapped_column(enum_column(Channel, 10))
    window_key: Mapped[str] = mapped_column(String(20))
    flush_at: Mapped[datetime]
    subject: Mapped[str | None] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    flushed_at: Mapped[datetime | None]
    digest_notification_id: Mapped[str | None] = mapped_column(String(32))

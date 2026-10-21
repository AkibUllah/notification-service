from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from notifications.core.channels import Channel, DigestMode, Priority


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


# --- tenants / users ------------------------------------------------------------------------


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=100_000)
    webhook_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2000)
    webhook_secret: str | None = Field(default=None, min_length=16, max_length=128)


class TenantOut(ORMModel):
    id: str
    name: str
    api_key_prefix: str
    rate_limit_per_minute: int
    webhook_url: str | None
    created_at: datetime


class TenantCreated(TenantOut):
    api_key: str
    webhook_secret: str | None


class UserUpsert(BaseModel):
    external_id: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, pattern=r"^\+?[0-9]{8,15}$")
    push_token: str | None = Field(default=None, max_length=500)
    webhook_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2000)
    locale: str | None = Field(default=None, pattern="^(en|bn)$")
    timezone: str | None = Field(default=None, max_length=60)
    quiet_hours_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    quiet_hours_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class PreferenceOut(ORMModel):
    category: str
    channels: dict[str, Any]
    digest: DigestMode
    updated_at: datetime


class UserOut(ORMModel):
    id: str
    external_id: str
    name: str | None
    email: str | None
    phone: str | None
    push_token: str | None
    webhook_url: str | None
    locale: str
    timezone: str
    quiet_hours_start: str | None
    quiet_hours_end: str | None
    created_at: datetime
    updated_at: datetime
    preferences: list[PreferenceOut]


class PreferenceSet(BaseModel):
    category: str = Field(default="*", min_length=1, max_length=60)
    channels: dict[Channel, bool] | None = None
    digest: DigestMode | None = None


class UnsubscribeTokenOut(BaseModel):
    token: str
    url: str
    category: str | None
    channel: str | None


# --- templates ------------------------------------------------------------------------------


class TemplateCreate(BaseModel):
    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    channel: Channel
    locale: str = Field(default="en", pattern="^(en|bn)$")
    subject: str | None = Field(default=None, max_length=500)
    body: str = Field(min_length=1)
    variables: list[str] = Field(default_factory=list)


class TemplateOut(ORMModel):
    id: str
    key: str
    channel: Channel
    locale: str
    version: int
    subject: str | None
    body: str
    variables: list[Any]
    active: bool
    created_by: str | None
    created_at: datetime


class RenderRequest(BaseModel):
    key: str
    channel: Channel
    locale: str = Field(default="en", pattern="^(en|bn)$")
    version: int | None = Field(default=None, ge=1)
    variables: dict[str, Any] = Field(default_factory=dict)
    user_external_id: str | None = None


class RenderOut(BaseModel):
    subject: str | None
    body: str
    template_id: str
    version: int
    locale: str


# --- notifications --------------------------------------------------------------------------


class NotificationCreate(BaseModel):
    user: str = Field(description="The tenant's external user id")
    category: str = Field(min_length=1, max_length=60)
    template: str = Field(min_length=1, max_length=100)
    variables: dict[str, Any] = Field(default_factory=dict)
    channels: list[Channel] | None = None
    priority: Priority = Priority.NORMAL
    idempotency_key: str | None = Field(default=None, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BulkCreate(BaseModel):
    notifications: list[NotificationCreate] = Field(min_length=1, max_length=500)


class DeliveryOut(ORMModel):
    id: str
    notification_id: str
    channel: Channel
    status: str
    skip_reason: str | None
    attempts: int
    next_attempt_at: datetime | None
    provider: str | None
    provider_message_id: str | None
    rendered_subject: str | None
    last_error: str | None
    receipt_status: str | None
    sent_at: datetime | None
    created_at: datetime


class NotificationOut(ORMModel):
    id: str
    user_id: str
    category: str
    template_key: str
    variables: dict[str, Any]
    priority: Priority
    channels_requested: list[Any]
    idempotency_key: str | None
    status: str
    created_at: datetime
    deliveries: list[DeliveryOut]


class BulkResult(BaseModel):
    accepted: list[NotificationOut]
    errors: list[dict[str, Any]]


class ReceiptIn(BaseModel):
    provider_message_id: str
    status: str = Field(pattern="^(delivered|bounced|failed|read|clicked|unsubscribed)$")


# --- inbox / ops ----------------------------------------------------------------------------


class InboxMessageOut(ORMModel):
    id: str
    notification_id: str | None
    category: str
    title: str | None
    body: str
    data: dict[str, Any]
    read_at: datetime | None
    created_at: datetime


class MarkRead(BaseModel):
    ids: list[str] | None = Field(default=None, description="Omit to mark everything read")


class WorkerRunOut(BaseModel):
    processed: int
    sent: int
    retried: int
    dead: int
    by_channel: dict[str, int]
    digests_flushed: int


class HealthOut(BaseModel):
    status: str
    database: str
    worker: str
    providers: list[dict[str, Any]]
    inbox_subscribers: int
    clock: datetime
    version: str

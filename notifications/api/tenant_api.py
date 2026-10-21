"""Tenant-facing API: users, preferences, templates, notifications, deliveries, receipts."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from notifications.api.deps import get_ctx, get_db, now, tenant
from notifications.core.channels import Channel
from notifications.models import Tenant
from notifications.schemas import (
    BulkCreate,
    BulkResult,
    DeliveryOut,
    NotificationCreate,
    NotificationOut,
    PreferenceOut,
    PreferenceSet,
    ReceiptIn,
    RenderOut,
    RenderRequest,
    TemplateCreate,
    TemplateOut,
    UnsubscribeTokenOut,
    UserOut,
    UserUpsert,
)
from notifications.services import notifications as notif
from notifications.services import preferences, templates, tenants, worker
from notifications.services.context import AppContext
from notifications.services.errors import DomainError

router = APIRouter(prefix="/v1", tags=["tenant"])


# users --------------------------------------------------------------------------------------


@router.put("/users", response_model=UserOut)
def upsert_user(
    body: UserUpsert,
    t: Tenant = Depends(tenant),
    db: Session = Depends(get_db),
    ctx: AppContext = Depends(get_ctx),
    at: datetime = Depends(now),
):
    data = body.model_dump(exclude_none=True)
    external_id = data.pop("external_id")
    return tenants.upsert_user(
        db,
        t,
        external_id=external_id,
        now=at,
        default_locale=ctx.settings.default_locale,
        default_timezone=ctx.settings.default_timezone,
        **data,
    )


@router.get("/users", response_model=list[UserOut])
def list_users(
    t: Tenant = Depends(tenant),
    db: Session = Depends(get_db),
    limit: int = Query(100, le=500),
    offset: int = 0,
):
    return tenants.list_users(db, t, limit=limit, offset=offset)


@router.get("/users/{external_id}", response_model=UserOut)
def get_user(external_id: str, t: Tenant = Depends(tenant), db: Session = Depends(get_db)):
    return tenants.get_user(db, t, external_id)


@router.put("/users/{external_id}/preferences", response_model=list[PreferenceOut])
def set_preference(
    external_id: str,
    body: PreferenceSet,
    t: Tenant = Depends(tenant),
    db: Session = Depends(get_db),
    at: datetime = Depends(now),
):
    user = tenants.get_user(db, t, external_id)
    channels = {c.value: v for c, v in body.channels.items()} if body.channels else None
    preferences.set_preference(
        db, user, body.category, channels=channels, digest=body.digest, now=at
    )
    return user.preferences


@router.post("/users/{external_id}/unsubscribe-token", response_model=UnsubscribeTokenOut)
def unsubscribe_token(
    external_id: str,
    t: Tenant = Depends(tenant),
    db: Session = Depends(get_db),
    ctx: AppContext = Depends(get_ctx),
    at: datetime = Depends(now),
    category: str | None = None,
    channel: Channel | None = None,
):
    user = tenants.get_user(db, t, external_id)
    token = preferences.issue_unsubscribe_token(
        db, user, category=category, channel=channel, now=at
    )
    return UnsubscribeTokenOut(
        token=token.token,
        url=f"{ctx.settings.public_base_url.rstrip('/')}/v1/unsubscribe/{token.token}",
        category=token.category,
        channel=token.channel,
    )


# templates ----------------------------------------------------------------------------------


@router.post("/templates", response_model=TemplateOut, status_code=201)
def create_template(
    body: TemplateCreate,
    t: Tenant = Depends(tenant),
    db: Session = Depends(get_db),
    at: datetime = Depends(now),
):
    return templates.create_template(db, t, now=at, **body.model_dump())


@router.get("/templates", response_model=list[TemplateOut])
def list_templates(
    t: Tenant = Depends(tenant), db: Session = Depends(get_db), key: str | None = None
):
    return templates.list_templates(db, t, key=key)


@router.delete("/templates/{template_id}", response_model=TemplateOut)
def deactivate_template(
    template_id: str, t: Tenant = Depends(tenant), db: Session = Depends(get_db)
):
    return templates.deactivate(db, t, template_id)


@router.post("/templates/render", response_model=RenderOut)
def render_preview(
    body: RenderRequest,
    t: Tenant = Depends(tenant),
    db: Session = Depends(get_db),
    ctx: AppContext = Depends(get_ctx),
    at: datetime = Depends(now),
):
    template = templates.resolve(
        db,
        t,
        body.key,
        body.channel,
        body.locale,
        version=body.version,
        default_locale=ctx.settings.default_locale,
    )
    user = tenants.get_user(db, t, body.user_external_id) if body.user_external_id else None
    rendered = templates.render(
        template,
        body.variables,
        user=user,
        now=at,
        unsubscribe_url=f"{ctx.settings.public_base_url}/v1/unsubscribe/preview",
    )
    return RenderOut(**rendered.__dict__)


# notifications ------------------------------------------------------------------------------


def _enqueue(db: Session, ctx: AppContext, t: Tenant, body: NotificationCreate, at: datetime):
    user = tenants.get_user(db, t, body.user)
    return notif.enqueue(
        db,
        ctx,
        t,
        user,
        category=body.category,
        template_key=body.template,
        variables=body.variables,
        now=at,
        channels=body.channels,
        priority=body.priority,
        idempotency_key=body.idempotency_key,
        metadata=body.metadata,
    )


@router.post("/notifications", response_model=NotificationOut, status_code=202)
def create_notification(
    body: NotificationCreate,
    response: Response,
    t: Tenant = Depends(tenant),
    db: Session = Depends(get_db),
    ctx: AppContext = Depends(get_ctx),
    at: datetime = Depends(now),
):
    """Queue one notification. Replaying an `idempotency_key` returns the original with 200."""
    notification, created = _enqueue(db, ctx, t, body, at)
    if not created:
        response.status_code = 200
        response.headers["X-Idempotent-Replay"] = "true"
    return notification


@router.post("/notifications/bulk", response_model=BulkResult, status_code=202)
def create_bulk(
    body: BulkCreate,
    t: Tenant = Depends(tenant),
    db: Session = Depends(get_db),
    ctx: AppContext = Depends(get_ctx),
    at: datetime = Depends(now),
):
    """Queue many; each item succeeds or fails on its own."""
    accepted, errors = [], []
    for index, item in enumerate(body.notifications):
        try:
            with db.begin_nested():
                notification, _ = _enqueue(db, ctx, t, item, at)
            accepted.append(notification)
        except DomainError as exc:
            errors.append(
                {"index": index, "user": item.user, "code": exc.code, "message": exc.message}
            )
    return BulkResult(accepted=accepted, errors=errors)


@router.get("/notifications", response_model=list[NotificationOut])
def list_notifications(
    response: Response,
    t: Tenant = Depends(tenant),
    db: Session = Depends(get_db),
    user: str | None = None,
    status: str | None = None,
    limit: int = Query(100, le=500),
    offset: int = 0,
):
    user_id = tenants.get_user(db, t, user).id if user else None
    rows, total = notif.list_notifications(
        db, t, user_id=user_id, status=status, limit=limit, offset=offset
    )
    response.headers["X-Total-Count"] = str(total)
    return rows


@router.get("/notifications/{notification_id}", response_model=NotificationOut)
def get_notification(
    notification_id: str, t: Tenant = Depends(tenant), db: Session = Depends(get_db)
):
    return notif.get_notification(db, t, notification_id)


@router.get("/deliveries", response_model=list[DeliveryOut])
def list_deliveries(
    t: Tenant = Depends(tenant),
    db: Session = Depends(get_db),
    status: str | None = None,
    channel: Channel | None = None,
    limit: int = Query(100, le=1000),
):
    return notif.list_deliveries(db, t, status=status, channel=channel, limit=limit)


@router.post("/receipts", response_model=DeliveryOut)
def receipt(
    body: ReceiptIn,
    t: Tenant = Depends(tenant),
    db: Session = Depends(get_db),
    at: datetime = Depends(now),
):
    """Delivery receipt from a provider callback relayed by the tenant (or a provider adapter)."""
    delivery = worker.record_receipt(db, body.provider_message_id, body.status, at)
    if delivery.tenant_id != t.id:
        raise DomainError("delivery belongs to another tenant", code="forbidden")
    return delivery

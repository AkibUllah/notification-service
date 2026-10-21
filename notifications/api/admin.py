"""Admin and ops endpoints: tenants, provider health, dead letters, manual worker runs."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from notifications.api.deps import get_ctx, get_db, now
from notifications.core.channels import Channel
from notifications.models import Delivery
from notifications.schemas import DeliveryOut, TenantCreate, TenantCreated, TenantOut, WorkerRunOut
from notifications.services import digests, tenants, worker
from notifications.services.context import AppContext
from notifications.services.errors import NotFound

router = APIRouter(prefix="/v1", tags=["admin"])


@router.post("/tenants", response_model=TenantCreated, status_code=201)
def create_tenant(
    body: TenantCreate,
    db: Session = Depends(get_db),
    ctx: AppContext = Depends(get_ctx),
    at: datetime = Depends(now),
):
    tenant, api_key = tenants.create_tenant(
        db,
        name=body.name,
        now=at,
        rate_limit_per_minute=body.rate_limit_per_minute
        or ctx.settings.default_rate_limit_per_minute,
        webhook_url=body.webhook_url,
        webhook_secret=body.webhook_secret,
    )
    return TenantCreated(
        **TenantOut.model_validate(tenant).model_dump(),
        api_key=api_key,
        webhook_secret=tenant.webhook_secret,
    )


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(db: Session = Depends(get_db)):
    return tenants.list_tenants(db)


@router.get("/providers")
def providers(ctx: AppContext = Depends(get_ctx)):
    return [chain.snapshot() for chain in ctx.chains.values()]


@router.get("/dead-letters", response_model=list[DeliveryOut])
def dead_letters(
    db: Session = Depends(get_db), channel: Channel | None = None, limit: int = Query(100, le=1000)
):
    stmt = select(Delivery).where(Delivery.status == "dead")
    if channel:
        stmt = stmt.where(Delivery.channel == channel)
    return list(db.scalars(stmt.order_by(Delivery.updated_at.desc()).limit(limit)))


@router.post("/deliveries/{delivery_id}/replay", response_model=DeliveryOut)
def replay(delivery_id: str, db: Session = Depends(get_db), at: datetime = Depends(now)):
    delivery = db.get(Delivery, delivery_id)
    if delivery is None:
        raise NotFound(f"delivery {delivery_id!r} not found")
    return worker.replay(db, delivery, at)


@router.post("/worker/run-once", response_model=WorkerRunOut)
async def run_once(
    db: Session = Depends(get_db),
    ctx: AppContext = Depends(get_ctx),
    at: datetime = Depends(now),
    batch_size: int = Query(50, ge=1, le=1000),
):
    """Flush due digests and process due deliveries now (handy without the background worker)."""
    flushed = digests.flush_due(db, ctx, at)
    db.commit()
    stats = await worker.process_once(db, ctx, at, batch_size)
    return WorkerRunOut(**stats.as_dict(), digests_flushed=len(flushed))

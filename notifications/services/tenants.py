from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from notifications.models import Tenant, User
from notifications.services.errors import Conflict, NotFound, Unauthorized


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_tenant(
    session: Session,
    *,
    name: str,
    now: datetime,
    rate_limit_per_minute: int,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
) -> tuple[Tenant, str]:
    plaintext = f"ntk-{secrets.token_hex(20)}"
    tenant = Tenant(
        name=name,
        api_key_hash=hash_key(plaintext),
        api_key_prefix=plaintext[:12],
        rate_limit_per_minute=rate_limit_per_minute,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret or (secrets.token_urlsafe(32) if webhook_url else None),
        created_at=now,
    )
    session.add(tenant)
    session.flush()
    return tenant, plaintext


def list_tenants(session: Session) -> list[Tenant]:
    return list(session.scalars(select(Tenant).order_by(Tenant.created_at)))


def authenticate(session: Session, key: str | None) -> Tenant:
    if not key:
        raise Unauthorized("missing tenant API key")
    tenant = session.scalar(select(Tenant).where(Tenant.api_key_hash == hash_key(key)))
    if tenant is None:
        raise Unauthorized("invalid tenant API key")
    return tenant


def upsert_user(
    session: Session,
    tenant: Tenant,
    *,
    external_id: str,
    now: datetime,
    default_locale: str,
    default_timezone: str,
    **fields,
) -> User:
    user = session.scalar(
        select(User).where(User.tenant_id == tenant.id, User.external_id == external_id)
    )
    created = user is None
    if created:
        user = User(
            tenant_id=tenant.id,
            external_id=external_id,
            locale=default_locale,
            timezone=default_timezone,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
    for name, value in fields.items():
        if value is not None:
            setattr(user, name, value)
    if (
        user.quiet_hours_start
        and not user.quiet_hours_end
        or user.quiet_hours_end
        and not user.quiet_hours_start
    ):
        raise Conflict(
            "quiet_hours_start and quiet_hours_end must be set together", code="invalid_quiet_hours"
        )
    user.updated_at = now
    session.flush()
    return user


def get_user(session: Session, tenant: Tenant, external_id: str) -> User:
    user = session.scalar(
        select(User).where(User.tenant_id == tenant.id, User.external_id == external_id)
    )
    if user is None:
        user = session.get(User, external_id)
        if user is not None and user.tenant_id != tenant.id:
            user = None
    if user is None:
        raise NotFound(f"user {external_id!r} not found")
    return user


def list_users(session: Session, tenant: Tenant, limit: int = 100, offset: int = 0) -> list[User]:
    return list(
        session.scalars(
            select(User)
            .where(User.tenant_id == tenant.id)
            .order_by(User.created_at, User.id)
            .limit(limit)
            .offset(offset)
        )
    )

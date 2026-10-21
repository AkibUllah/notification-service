from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from notifications.clock import ensure_utc
from notifications.config import Settings
from notifications.models import Tenant
from notifications.services import tenants
from notifications.services.context import AppContext


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_db(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def now(ctx: AppContext = Depends(get_ctx)) -> datetime:
    return ensure_utc(ctx.clock.now())


def require_admin(
    settings: Settings = Depends(get_settings), x_api_key: str | None = Header(default=None)
) -> None:
    if settings.admin_api_key and x_api_key != settings.admin_api_key:
        raise HTTPException(status_code=401, detail="invalid or missing admin API key")


def tenant(db: Session = Depends(get_db), x_api_key: str | None = Header(default=None)) -> Tenant:
    return tenants.authenticate(db, x_api_key)

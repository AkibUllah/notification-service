"""In-app inbox: list, mark read, unread count, SSE stream. Plus the public unsubscribe link."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from notifications.api.deps import get_ctx, get_db, now, tenant
from notifications.models import Tenant
from notifications.schemas import InboxMessageOut, MarkRead
from notifications.services import inbox, preferences, tenants
from notifications.services.bus import InboxBus
from notifications.services.context import AppContext

router = APIRouter(prefix="/v1", tags=["inbox"])
public = APIRouter(prefix="/v1", tags=["public"])


@router.get("/inbox/{external_id}", response_model=list[InboxMessageOut])
def list_inbox(
    external_id: str,
    t: Tenant = Depends(tenant),
    db: Session = Depends(get_db),
    unread: bool = False,
    limit: int = Query(50, le=200),
    before: datetime | None = None,
):
    return inbox.list_messages(
        db, tenants.get_user(db, t, external_id), unread_only=unread, limit=limit, before=before
    )


@router.get("/inbox/{external_id}/unread-count")
def unread(external_id: str, t: Tenant = Depends(tenant), db: Session = Depends(get_db)):
    return {"unread_count": inbox.unread_count(db, tenants.get_user(db, t, external_id))}


@router.post("/inbox/{external_id}/read")
def mark_read(
    external_id: str,
    body: MarkRead | None = None,
    t: Tenant = Depends(tenant),
    db: Session = Depends(get_db),
    ctx: AppContext = Depends(get_ctx),
    at: datetime = Depends(now),
):
    user = tenants.get_user(db, t, external_id)
    count = inbox.mark_read(db, ctx.bus, user, ids=(body.ids if body else None), now=at)
    return {"marked": count, "unread_count": inbox.unread_count(db, user)}


def format_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def inbox_events(
    bus: InboxBus,
    user_id: str,
    initial: dict,
    *,
    heartbeat_seconds: float,
    max_events: int | None = None,
    disconnected=None,
) -> AsyncIterator[str]:
    """`ready` with the current unread count, then `message` / `read` events as they happen."""
    sent = 0
    yield format_sse("ready", initial)
    sent += 1
    if max_events is not None and sent >= max_events:
        return
    with bus.subscribe(user_id) as queue:
        while True:
            if disconnected is not None and await disconnected():
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield format_sse(event.get("type", "message"), event)
            sent += 1
            if max_events is not None and sent >= max_events:
                return


@router.get("/inbox/{external_id}/stream")
async def stream(
    request: Request,
    external_id: str,
    t: Tenant = Depends(tenant),
    db: Session = Depends(get_db),
    ctx: AppContext = Depends(get_ctx),
    max_events: int | None = Query(default=None, ge=1, le=1000),
):
    user = tenants.get_user(db, t, external_id)
    initial = {"unread_count": inbox.unread_count(db, user), "user": user.external_id}
    generator = inbox_events(
        ctx.bus,
        user.id,
        initial,
        heartbeat_seconds=15.0,
        max_events=max_events,
        disconnected=request.is_disconnected,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@public.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe(token: str, db: Session = Depends(get_db), at: datetime = Depends(now)):
    record = preferences.unsubscribe(db, token, at)
    what = (
        f"{record.channel} notifications" if record.channel else "email, SMS and push notifications"
    )
    scope = f" for '{record.category}'" if record.category else ""
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>Unsubscribed</title>"
        "<main style='font-family:system-ui;max-width:32rem;margin:4rem auto'>"
        f"<h1>You're unsubscribed</h1><p>You will no longer receive {what}{scope}. "
        "You can change this any time in your account settings.</p></main>"
    )

"""Tell the tenant what happened to a delivery: signed POST to its webhook URL."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from notifications.core.signing import HEADER, sign
from notifications.models import Delivery, Tenant
from notifications.services.context import AppContext

log = logging.getLogger(__name__)


def delivery_payload(delivery: Delivery) -> dict:
    return {
        "delivery_id": delivery.id,
        "notification_id": delivery.notification_id,
        "user_id": delivery.user_id,
        "channel": delivery.channel.value,
        "status": delivery.status,
        "provider": delivery.provider,
        "provider_message_id": delivery.provider_message_id,
        "attempts": delivery.attempts,
        "receipt_status": delivery.receipt_status,
        "last_error": delivery.last_error,
        "sent_at": delivery.sent_at.isoformat() if delivery.sent_at else None,
    }


async def notify(
    ctx: AppContext, tenant: Tenant, event_type: str, delivery: Delivery, now: datetime
) -> bool:
    if not tenant.webhook_url or not tenant.webhook_secret:
        return False
    body = json.dumps(
        {"type": event_type, "created_at": now.isoformat(), "data": delivery_payload(delivery)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    headers = {
        "Content-Type": "application/json",
        HEADER: sign(tenant.webhook_secret, int(now.timestamp()), body),
        "X-Notify-Event": event_type,
    }
    try:
        response = await ctx.http.post(tenant.webhook_url, content=body, headers=headers)
        return response.is_success
    except Exception as exc:  # status webhooks are best-effort
        log.warning("status webhook to %s failed: %s", tenant.webhook_url, exc)
        return False

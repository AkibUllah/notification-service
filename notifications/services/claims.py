"""Claiming due deliveries so several worker replicas can share one queue.

On PostgreSQL the due rows are selected with ``FOR UPDATE SKIP LOCKED``: a second worker
running the same query at the same moment skips the rows the first one has locked instead of
waiting for them or picking them too. In the same transaction the rows move to ``sending``
with ``claimed_by`` and ``lease_expires_at`` and the transaction commits, so row locks are
held for milliseconds, not for as long as the sends take.

SQLite has no row locks, so the select runs without them. That is correct for the single
worker SQLite is meant for, and the claim UPDATE still refuses rows that stopped being
``pending`` in the meantime.

Results are written back with a guarded UPDATE (``claimed_by`` must still be this claim's
token and the row must still be ``sending``). A worker whose lease expired and whose rows
were reclaimed by another worker therefore cannot overwrite the other worker's result.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from notifications.models import Delivery

# Columns `worker.deliver` may change; written back by `complete` under the claim guard.
RESULT_COLUMNS = (
    "status",
    "attempts",
    "next_attempt_at",
    "provider",
    "provider_message_id",
    "rendered_subject",
    "rendered_body",
    "last_error",
    "sent_at",
    "updated_at",
)


@dataclass
class Claim:
    token: str  # "<worker id>#<random>", stored in `claimed_by`
    lease_expires_at: datetime
    deliveries: list[Delivery]


def supports_skip_locked(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def _queue_order(stmt: Select) -> Select:
    return stmt.order_by(Delivery.priority_rank, Delivery.next_attempt_at, Delivery.id)


def claim_due(
    session: Session, now: datetime, limit: int, worker_id: str, lease_seconds: float
) -> Claim:
    """Claim up to `limit` due deliveries for `worker_id` and commit the claim."""
    claim = Claim(
        token=f"{worker_id}#{secrets.token_hex(4)}",
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        deliveries=[],
    )
    due = _queue_order(
        select(Delivery.id).where(
            Delivery.status == "pending",
            Delivery.next_attempt_at.is_not(None),
            Delivery.next_attempt_at <= now,
        )
    ).limit(limit)
    if supports_skip_locked(session):
        due = due.with_for_update(skip_locked=True)
    ids = list(session.scalars(due))
    if ids:
        session.execute(
            update(Delivery)
            .where(Delivery.id.in_(ids), Delivery.status == "pending")
            .values(
                status="sending",
                claimed_by=claim.token,
                lease_expires_at=claim.lease_expires_at,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
    session.commit()
    if ids:
        claim.deliveries = list(
            session.scalars(
                _queue_order(
                    select(Delivery).where(Delivery.claimed_by == claim.token)
                ).execution_options(populate_existing=True)
            )
        )
    return claim


def complete(session: Session, delivery: Delivery, token: str) -> bool:
    """Write the attempt's result if `token` still holds the claim. Returns False (and writes
    nothing) when the lease was lost; the caller must roll back its other changes."""
    values = {name: getattr(delivery, name) for name in RESULT_COLUMNS}
    values.update(claimed_by=None, lease_expires_at=None)
    result = session.execute(
        update(Delivery)
        .where(
            Delivery.id == delivery.id,
            Delivery.claimed_by == token,
            Delivery.status == "sending",
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    for name, value in values.items():
        set_committed_value(delivery, name, value)
    return True


def release(session: Session, token: str, delivery_ids: list[str], now: datetime) -> int:
    """Hand unprocessed rows of a claim back to the queue without counting an attempt."""
    if not delivery_ids:
        return 0
    result = session.execute(
        update(Delivery)
        .where(
            Delivery.id.in_(delivery_ids),
            Delivery.claimed_by == token,
            Delivery.status == "sending",
        )
        .values(status="pending", claimed_by=None, lease_expires_at=None, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return result.rowcount


def expired(session: Session, now: datetime, limit: int) -> list[Delivery]:
    """Claimed rows whose lease ran out, locked (PostgreSQL) so only one worker reclaims each."""
    stmt = (
        select(Delivery)
        .where(Delivery.status == "sending", Delivery.lease_expires_at <= now)
        .order_by(Delivery.lease_expires_at, Delivery.id)
        .limit(limit)
    )
    if supports_skip_locked(session):
        stmt = stmt.with_for_update(skip_locked=True)
    return list(session.scalars(stmt.execution_options(populate_existing=True)))

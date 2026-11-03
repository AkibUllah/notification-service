from datetime import timedelta

from sqlalchemy import update

from notifications.core.channels import Channel, Priority
from notifications.models import Delivery
from notifications.services import claims, inbox
from notifications.services.notifications import summarize
from notifications.services.worker import RunStats, process_claim, process_once
from tests.conftest import T0
from tests.test_worker import enqueue


def status_events(receiver):
    return [r for r in receiver.requests if r.url.path == "/notify-status"]


def test_claim_marks_rows_sending_in_queue_order(session, ctx, tenant, user, order_templates):
    low = enqueue(session, ctx, tenant, user, channels=[Channel.SMS], priority=Priority.LOW)
    high = enqueue(session, ctx, tenant, user, channels=[Channel.SMS], priority=Priority.HIGH)
    later = enqueue(session, ctx, tenant, user, channels=[Channel.SMS], now=T0 + timedelta(hours=1))

    claim = claims.claim_due(session, T0, 10, "worker-a", 120)
    assert claim.token.startswith("worker-a#") and claim.lease_expires_at == T0 + timedelta(
        seconds=120
    )
    assert [d.notification_id for d in claim.deliveries] == [high.id, low.id]
    assert all(
        d.status == "sending"
        and d.claimed_by == claim.token
        and d.lease_expires_at == claim.lease_expires_at
        for d in claim.deliveries
    )
    assert later.deliveries[0].status == "pending" and later.deliveries[0].claimed_by is None
    session.refresh(high)
    assert summarize(high) == "queued"

    # Claimed rows are not pending any more, so nobody else can take them.
    again = claims.claim_due(session, T0, 10, "worker-b", 120)
    assert again.deliveries == [] and again.token != claim.token


def test_claim_respects_limit_and_reports_sqlite_has_no_skip_locked(
    session, ctx, tenant, user, order_templates
):
    for _ in range(3):
        enqueue(session, ctx, tenant, user, channels=[Channel.SMS])
    assert claims.supports_skip_locked(session) is False
    assert len(claims.claim_due(session, T0, 2, "worker-a", 60).deliveries) == 2
    assert len(claims.claim_due(session, T0, 2, "worker-a", 60).deliveries) == 1
    assert claims.claim_due(session, T0, 2, "worker-a", 60).deliveries == []


async def test_completion_clears_the_claim(session, ctx, tenant, user, order_templates, fakes):
    n = enqueue(session, ctx, tenant, user, channels=[Channel.SMS])
    stats = await process_once(session, ctx, T0, worker_id="worker-a")
    (d,) = n.deliveries
    session.refresh(d)
    assert stats.sent == 1 and stats.lease_lost == 0
    assert d.status == "sent" and d.claimed_by is None and d.lease_expires_at is None


async def test_lost_claim_cannot_overwrite_the_new_owner(
    session, ctx, tenant, user, order_templates, fakes, receiver
):
    n = enqueue(session, ctx, tenant, user, channels=[Channel.SMS, Channel.IN_APP])
    claim = claims.claim_due(session, T0, 10, "worker-a", 60)
    assert len(claim.deliveries) == 2
    # Meanwhile the rows were reclaimed and claimed again by another worker.
    session.execute(update(Delivery).values(claimed_by="worker-b#0000"))
    session.commit()

    stats = RunStats()
    await process_claim(session, ctx, claim, T0, stats)
    assert stats.lease_lost == 2 and stats.processed == 0 and stats.sent == 0
    assert len(fakes.sms.sent) == 1  # the send itself already happened; only the write is refused
    for d in n.deliveries:
        session.refresh(d)
        assert d.status == "sending" and d.claimed_by == "worker-b#0000" and d.attempts == 0
    session.refresh(n)
    assert n.status == "queued" and status_events(receiver) == []
    assert inbox.unread_count(session, user) == 0  # the in-app insert was rolled back


async def test_expired_lease_mid_batch_hands_the_rest_back(
    session, ctx, tenant, user, order_templates, fakes, clock
):
    enqueue(session, ctx, tenant, user, channels=[Channel.SMS, Channel.EMAIL])
    claim = claims.claim_due(session, T0, 10, "worker-a", 60)
    clock.advance(seconds=60)

    stats = RunStats()
    await process_claim(session, ctx, claim, T0, stats)
    assert stats.processed == 0 and fakes.sms.sent == [] and fakes.email.sent == []
    for d in claim.deliveries:
        session.refresh(d)
        assert d.status == "pending" and d.claimed_by is None and d.attempts == 0
    # Handed back rows are due again immediately.
    assert (await process_once(session, ctx, T0 + timedelta(seconds=60))).sent == 2


def test_release_only_touches_rows_of_its_own_claim(session, ctx, tenant, user, order_templates):
    enqueue(session, ctx, tenant, user, channels=[Channel.SMS])
    claim = claims.claim_due(session, T0, 10, "worker-a", 60)
    ids = [d.id for d in claim.deliveries]
    assert claims.release(session, "worker-b#ffff", ids, T0) == 0
    assert claims.release(session, claim.token, [], T0) == 0
    assert claims.release(session, claim.token, ids, T0) == 1
    assert claims.release(session, claim.token, ids, T0) == 0

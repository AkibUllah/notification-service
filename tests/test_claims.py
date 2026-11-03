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


async def test_crashed_worker_lease_expires_and_another_worker_delivers(
    session, ctx, tenant, user, order_templates, fakes, receiver
):
    n = enqueue(session, ctx, tenant, user, channels=[Channel.SMS])
    claim = claims.claim_due(session, T0, 10, "worker-a", ctx.settings.worker_lease_seconds)
    lease_end = T0 + timedelta(seconds=300)
    assert claim.lease_expires_at == lease_end
    # worker-a dies here without completing.

    early = await process_once(session, ctx, lease_end - timedelta(seconds=1), worker_id="worker-b")
    assert early.reclaimed == 0 and early.processed == 0

    stats = await process_once(session, ctx, lease_end, worker_id="worker-b")
    (d,) = n.deliveries
    session.refresh(d)
    assert stats.reclaimed == 1 and stats.processed == 0  # back off before retrying
    assert d.status == "pending" and d.attempts == 1 and d.claimed_by is None
    assert d.last_error == f"lease expired (claimed by {claim.token})"
    assert d.next_attempt_at == lease_end + timedelta(seconds=30)

    stats = await process_once(
        session, ctx, lease_end + timedelta(seconds=30), worker_id="worker-b"
    )
    session.refresh(d)
    assert stats.sent == 1 and d.status == "sent" and d.attempts == 2 and len(fakes.sms.sent) == 1

    # worker-a wakes up late: its lease is gone, so it neither sends nor overwrites anything.
    ctx.clock.set(lease_end + timedelta(minutes=1))
    late = RunStats()
    await process_claim(session, ctx, claim, lease_end + timedelta(minutes=1), late)
    session.refresh(d)
    assert late.processed == 0 and len(fakes.sms.sent) == 1
    assert d.status == "sent" and d.attempts == 2


async def test_repeated_lease_expiry_dead_letters_the_delivery(
    session, ctx, tenant, user, order_templates, fakes, receiver
):
    n = enqueue(session, ctx, tenant, user, channels=[Channel.SMS])
    (d,) = n.deliveries
    at = T0
    for attempt in range(1, 4):  # max_attempts=3 in the test settings
        claim = claims.claim_due(session, at, 10, f"crashy-{attempt}", 60)
        assert [x.id for x in claim.deliveries] == [d.id]
        at += timedelta(seconds=60)
        stats = await process_once(session, ctx, at, worker_id="worker-b")
        session.refresh(d)
        assert stats.reclaimed == 1 and d.attempts == attempt
        if attempt < 3:
            assert d.status == "pending"
            at = d.next_attempt_at
    assert stats.dead == 1 and d.status == "dead" and d.next_attempt_at is None
    assert fakes.sms.sent == []
    session.refresh(n)
    assert n.status == "failed"
    events = [r for r in receiver.requests if r.url.path == "/notify-status"]
    assert len(events) == 1 and b'"delivery.dead"' in events[0].content

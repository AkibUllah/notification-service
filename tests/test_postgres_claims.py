"""Multi-worker claiming against a real PostgreSQL.

Skipped unless TEST_DATABASE_URL points at a disposable database, for example
TEST_DATABASE_URL=postgresql+psycopg://notify:notify@localhost:5432/notify_test
The tests drop and recreate every table in it.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections import Counter

import pytest
from sqlalchemy import func, select, text

from notifications.clock import FixedClock
from notifications.config import Settings
from notifications.core.channels import Channel
from notifications.database import init_db, make_engine, make_session_factory
from notifications.models import Base, Delivery
from notifications.providers.base import Message, ProviderResult
from notifications.providers.chain import ProviderChain
from notifications.services import claims, notifications, templates, tenants
from notifications.services.context import AppContext
from notifications.services.worker import RunStats, process_once
from tests.conftest import T0, VARS

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to a PostgreSQL database"
)


class RecordingSMS:
    """Thread-safe provider that yields to the event loop on every send, so two workers
    really interleave, and records which worker sent which delivery."""

    name = "recording_sms"
    channel = Channel.SMS

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sent: list[tuple[str, str]] = []

    async def send(self, message: Message) -> ProviderResult:
        await asyncio.sleep(0.002)
        with self.lock:
            self.sent.append((threading.current_thread().name, message.delivery_id))
            return ProviderResult(True, provider_message_id=f"rec-{len(self.sent)}")


@pytest.fixture
def pg(tmp_path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=TEST_DATABASE_URL,
        worker_enabled=False,
        worker_batch_size=5,
        max_attempts=3,
        push_file_path=str(tmp_path / "push.jsonl"),
        log_level="WARNING",
    )
    engine = make_engine(settings.database_url)
    Base.metadata.drop_all(engine)
    init_db(engine)
    yield settings, make_session_factory(engine)
    Base.metadata.drop_all(engine)
    engine.dispose()


def make_ctx(settings: Settings, provider: RecordingSMS) -> AppContext:
    clock = FixedClock(T0)
    chain = ProviderChain(
        Channel.SMS, [provider], failure_threshold=100, cooldown_seconds=1, clock=clock
    )
    return AppContext.from_settings(settings, clock=clock, chains={Channel.SMS: chain})


def seed(settings: Settings, session_factory, provider: RecordingSMS, count: int) -> list[str]:
    ctx = make_ctx(settings, provider)
    with session_factory() as session:
        tenant, _ = tenants.create_tenant(
            session, name="Dhaka Grocers", now=T0, rate_limit_per_minute=100_000
        )
        user = tenants.upsert_user(
            session,
            tenant,
            external_id="u-42",
            now=T0,
            default_locale="en",
            default_timezone="Asia/Dhaka",
            phone="+8801711000000",
        )
        templates.create_template(
            session,
            tenant,
            key="order-shipped",
            channel="sms",
            locale="en",
            body="Order {{ order_id }} shipped. {{ amount }}",
            variables=["order_id", "amount"],
            now=T0,
        )
        for i in range(count):
            notifications.enqueue(
                session,
                ctx,
                tenant,
                user,
                category="orders",
                template_key="order-shipped",
                variables={**VARS, "order_id": f"ORD-{i}"},
                channels=[Channel.SMS],
                now=T0,
            )
        session.commit()
        return list(session.scalars(select(Delivery.id)))


def test_concurrent_workers_send_every_delivery_exactly_once(pg):
    settings, session_factory = pg
    provider = RecordingSMS()
    ids = seed(settings, session_factory, provider, count=200)
    workers = [f"worker-{i}" for i in range(4)]
    start = threading.Barrier(len(workers))
    results: dict[str, RunStats] = {}
    errors: list[BaseException] = []

    async def drain(worker_id: str) -> RunStats:
        total = RunStats()
        ctx = make_ctx(settings, provider)
        session = session_factory()
        try:
            start.wait()
            while True:
                stats = await process_once(session, ctx, T0, worker_id=worker_id)
                total.processed += stats.processed
                total.sent += stats.sent
                total.lease_lost += stats.lease_lost
                if stats.processed == 0:
                    return total
        finally:
            session.close()
            await ctx.http.aclose()

    def run(worker_id: str) -> None:
        try:
            results[worker_id] = asyncio.run(drain(worker_id))
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(w,), name=w) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors

    per_delivery = Counter(delivery_id for _, delivery_id in provider.sent)
    assert sorted(per_delivery) == sorted(ids)
    assert set(per_delivery.values()) == {1}, "a delivery was sent more than once"
    per_worker = Counter(worker for worker, _ in provider.sent)
    assert sum(1 for w in workers if per_worker[w]) >= 2, per_worker  # the work was shared
    assert sum(results[w].sent for w in workers) == len(ids)
    assert all(results[w].lease_lost == 0 for w in workers)

    with session_factory() as session:
        rows = session.execute(
            select(Delivery.status, Delivery.attempts, Delivery.claimed_by)
        ).all()
    assert {tuple(r) for r in rows} == {("sent", 1, None)}


def test_claim_skips_rows_locked_by_another_transaction(pg):
    settings, session_factory = pg
    ids = seed(settings, session_factory, RecordingSMS(), count=6)
    holder, other = session_factory(), session_factory()
    try:
        # `holder` is mid-claim: it has locked the first three rows and not committed yet.
        locked = list(
            holder.scalars(
                select(Delivery.id)
                .order_by(Delivery.priority_rank, Delivery.next_attempt_at, Delivery.id)
                .limit(3)
                .with_for_update()
            )
        )
        # Waiting on a lock would fail fast instead of passing slowly.
        other.execute(text("SET lock_timeout = '2s'"))
        claim = claims.claim_due(other, T0, 10, "worker-b", 60)
        claimed = [d.id for d in claim.deliveries]
        assert len(claimed) == 3 and not set(claimed) & set(locked)
        assert set(claimed) | set(locked) == set(ids)
    finally:
        holder.rollback()
        holder.close()
        other.close()


def test_only_one_worker_reclaims_an_expired_lease(pg):
    settings, session_factory = pg
    seed(settings, session_factory, RecordingSMS(), count=2)
    with session_factory() as session:
        claims.claim_due(session, T0, 10, "worker-a", 60)
    first, second = session_factory(), session_factory()
    try:
        later = T0.replace(minute=5)
        assert len(claims.expired(first, later, 10)) == 2
        second.execute(text("SET lock_timeout = '2s'"))
        assert claims.expired(second, later, 10) == []
    finally:
        first.rollback()
        first.close()
        second.close()
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Delivery).where(Delivery.status == "sending")
            )
            == 2
        )

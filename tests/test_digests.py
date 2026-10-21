from datetime import datetime, timedelta, timezone

from notifications.core.channels import Channel, DigestMode
from notifications.core.digest import flush_at, window_key
from notifications.services import digests, notifications, preferences, templates
from notifications.services.worker import process_once
from tests.conftest import T0, VARS


def test_window_keys_and_flush_times_in_local_time():
    t = datetime(2026, 1, 1, 6, 20, tzinfo=timezone.utc)  # 12:20 Dhaka
    assert window_key(t, DigestMode.HOURLY, "Asia/Dhaka") == "2026-01-01T12"
    assert flush_at(t, DigestMode.HOURLY, "Asia/Dhaka") == datetime(
        2026, 1, 1, 7, 0, tzinfo=timezone.utc
    )
    assert (
        window_key(t, DigestMode.DAILY, "Asia/Dhaka") == "2026-01-02"
    )  # after 09:00 -> tomorrow's digest
    assert flush_at(t, DigestMode.DAILY, "Asia/Dhaka") == datetime(
        2026, 1, 2, 3, 0, tzinfo=timezone.utc
    )  # 09:00 Dhaka
    early = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)  # 07:00 Dhaka
    assert window_key(early, DigestMode.DAILY, "Asia/Dhaka") == "2026-01-01"
    assert flush_at(early, DigestMode.DAILY, "Asia/Dhaka") == datetime(
        2026, 1, 1, 3, 0, tzinfo=timezone.utc
    )


async def test_hourly_digest_batches_and_flushes_once(
    session, ctx, tenant, user, order_templates, fakes
):
    preferences.set_preference(
        session, user, "orders", channels=None, digest=DigestMode.HOURLY, now=T0
    )
    for i in range(3):
        n, _ = notifications.enqueue(
            session,
            ctx,
            tenant,
            user,
            category="orders",
            template_key="order-shipped",
            variables={**VARS, "order_id": f"ORD-{i}"},
            now=T0 + timedelta(minutes=i),
            channels=[Channel.EMAIL, Channel.IN_APP],
        )
        assert n.status == "queued"  # in-app still goes out immediately
        assert {d.channel.value: d.status for d in n.deliveries} == {
            "email": "digested",
            "in_app": "pending",
        }
    assert len(digests.pending_entries(session, user)) == 3
    assert digests.flush_due(session, ctx, T0 + timedelta(minutes=59)) == []
    created = digests.flush_due(session, ctx, T0 + timedelta(hours=1))
    assert len(created) == 1
    digest = created[0]
    assert (
        digest.template_key == "digest"
        and digest.variables["count"] == 3
        and digest.metadata_["window_key"] == "2026-01-01T12"
    )
    (delivery,) = digest.deliveries
    assert delivery.rendered_subject == "You have 3 new notifications"
    assert delivery.rendered_body.count("- Order ORD-") == 3
    assert digests.pending_entries(session, user) == []
    assert digests.flush_due(session, ctx, T0 + timedelta(hours=2)) == []
    stats = await process_once(session, ctx, T0 + timedelta(hours=1))
    assert stats.sent >= 1 and fakes.email.sent[-1].subject == "You have 3 new notifications"


async def test_digest_uses_tenant_template_and_locale(session, ctx, tenant, user, order_templates):
    user.locale = "bn"
    templates.create_template(
        session,
        tenant,
        key="digest",
        channel="email",
        locale="bn",
        subject="{{ count }}টি আপডেট",
        body="{% for item in items %}* {{ item.subject }}\n{% endfor %}",
        variables=["items", "count", "category"],
        now=T0,
    )
    preferences.set_preference(
        session, user, "orders", channels=None, digest=DigestMode.DAILY, now=T0
    )
    notifications.enqueue(
        session,
        ctx,
        tenant,
        user,
        category="orders",
        template_key="order-shipped",
        variables=VARS,
        now=T0,
        channels=[Channel.EMAIL],
    )
    (digest,) = digests.flush_due(session, ctx, datetime(2026, 1, 2, 3, 0, tzinfo=timezone.utc))
    (delivery,) = digest.deliveries
    assert delivery.rendered_subject == "1টি আপডেট"
    assert "* অর্ডার ORD-4521 পাঠানো হয়েছে" in delivery.rendered_body


def test_digest_groups_per_user_category_and_channel(session, ctx, tenant, user, order_templates):
    preferences.set_preference(session, user, "*", channels=None, digest=DigestMode.HOURLY, now=T0)
    notifications.enqueue(
        session,
        ctx,
        tenant,
        user,
        category="orders",
        template_key="order-shipped",
        variables=VARS,
        now=T0,
        channels=[Channel.EMAIL, Channel.SMS],
    )
    notifications.enqueue(
        session,
        ctx,
        tenant,
        user,
        category="promo",
        template_key="order-shipped",
        variables=VARS,
        now=T0,
        channels=[Channel.EMAIL],
    )
    created = digests.flush_due(session, ctx, T0 + timedelta(hours=1))
    assert sorted((n.category, n.deliveries[0].channel.value) for n in created) == [
        ("orders", "email"),
        ("orders", "sms"),
        ("promo", "email"),
    ]

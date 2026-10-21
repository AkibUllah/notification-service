from datetime import datetime, timedelta, timezone

import pytest

from notifications.core.channels import Channel, DigestMode, Priority
from notifications.core.quiet_hours import in_quiet_hours, quiet_hours_end
from notifications.services import preferences
from notifications.services.errors import NotFound
from tests.conftest import T0

ALL = [Channel.IN_APP, Channel.EMAIL, Channel.SMS, Channel.PUSH, Channel.WEBHOOK]


def actions(res):
    return {d.channel.value: d.action for d in res.decisions}


def test_defaults_send_everywhere_the_user_has_an_address(session, user):
    res = preferences.resolve(user, "orders", ALL, Priority.NORMAL, T0)
    assert actions(res) == {c.value: "send" for c in ALL}
    user.phone = None
    res = preferences.resolve(user, "orders", ALL, Priority.NORMAL, T0)
    assert (
        res.by_action("skip")[0].channel is Channel.SMS
        and res.by_action("skip")[0].reason == "no_address"
    )


def test_category_preference_overrides_catch_all(session, user):
    preferences.set_preference(
        session, user, "*", channels={"sms": False, "email": False}, digest=None, now=T0
    )
    preferences.set_preference(
        session, user, "security", channels={"sms": True}, digest=None, now=T0
    )
    orders = actions(preferences.resolve(user, "orders", ALL, Priority.NORMAL, T0))
    assert orders["sms"] == "skip" and orders["email"] == "skip" and orders["push"] == "send"
    security = actions(preferences.resolve(user, "security", ALL, Priority.NORMAL, T0))
    assert (
        security["sms"] == "send" and security["email"] == "skip"
    )  # inherits email opt-out from *
    with pytest.raises(ValueError):
        preferences.set_preference(
            session, user, "*", channels={"pigeon": True}, digest=None, now=T0
        )


@pytest.mark.parametrize(
    ("utc_hour", "expected"),
    [
        (16, True),
        (17, True),
        (1, True),
        (2, False),
        (12, False),
        (15, False),
    ],  # 22:00-08:00 Dhaka == 16:00-02:00 UTC
)
def test_quiet_hours_overnight_window_in_dhaka(utc_hour, expected):
    now = datetime(2026, 1, 1, utc_hour, 30, tzinfo=timezone.utc)
    assert in_quiet_hours(now, "22:00", "08:00", "Asia/Dhaka") is expected


def test_quiet_hours_same_day_window_and_end_time():
    assert in_quiet_hours(
        datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc), "13:00", "15:00", "Asia/Dhaka"
    )  # 14:00 local
    assert not in_quiet_hours(
        datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc), "13:00", "15:00", "Asia/Dhaka"
    )
    assert not in_quiet_hours(T0, None, None, "Asia/Dhaka")
    assert not in_quiet_hours(T0, "09:00", "09:00", "Asia/Dhaka")
    late = datetime(2026, 1, 1, 17, 0, tzinfo=timezone.utc)  # 23:00 Dhaka
    assert quiet_hours_end(late, "22:00", "08:00", "Asia/Dhaka") == datetime(
        2026, 1, 2, 2, 0, tzinfo=timezone.utc
    )
    early = datetime(2026, 1, 2, 0, 30, tzinfo=timezone.utc)  # 06:30 Dhaka
    assert quiet_hours_end(early, "22:00", "08:00", "Asia/Dhaka") == datetime(
        2026, 1, 2, 2, 0, tzinfo=timezone.utc
    )


def test_quiet_hours_defer_external_channels_unless_high_priority(session, user):
    user.quiet_hours_start, user.quiet_hours_end = "22:00", "08:00"
    night = datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)  # 00:00 Dhaka
    res = preferences.resolve(user, "orders", ALL, Priority.NORMAL, night)
    assert actions(res) == {
        "in_app": "send",
        "email": "defer",
        "sms": "defer",
        "push": "defer",
        "webhook": "send",
    }
    assert all(
        d.deliver_at == datetime(2026, 1, 2, 2, 0, tzinfo=timezone.utc)
        for d in res.by_action("defer")
    )
    otp = preferences.resolve(user, "security", [Channel.SMS], Priority.HIGH, night)
    assert actions(otp) == {"sms": "send"}


def test_digest_mode_batches_external_channels(session, user):
    preferences.set_preference(
        session, user, "marketing", channels=None, digest=DigestMode.DAILY, now=T0
    )
    res = preferences.resolve(user, "marketing", ALL, Priority.NORMAL, T0)
    assert actions(res) == {
        "in_app": "send",
        "email": "digest",
        "sms": "digest",
        "push": "digest",
        "webhook": "send",
    }
    assert res.by_action("digest")[0].digest is DigestMode.DAILY
    urgent = preferences.resolve(user, "marketing", [Channel.EMAIL], Priority.HIGH, T0)
    assert actions(urgent) == {"email": "send"}


def test_unsubscribe_token_flow(session, user):
    token = preferences.issue_unsubscribe_token(
        session, user, category="marketing", channel=Channel.EMAIL, now=T0
    )
    preferences.unsubscribe(session, token.token, T0 + timedelta(hours=1))
    assert token.used_at == T0 + timedelta(hours=1)
    assert not preferences.channel_allowed(user, "marketing", Channel.EMAIL)
    assert preferences.channel_allowed(user, "marketing", Channel.SMS)
    assert preferences.channel_allowed(user, "orders", Channel.EMAIL)
    everything = preferences.issue_unsubscribe_token(
        session, user, category=None, channel=None, now=T0
    )
    preferences.unsubscribe(session, everything.token, T0)
    assert all(
        not preferences.channel_allowed(user, "orders", c)
        for c in (Channel.EMAIL, Channel.SMS, Channel.PUSH)
    )
    assert preferences.channel_allowed(user, "orders", Channel.IN_APP)
    with pytest.raises(NotFound):
        preferences.unsubscribe(session, "nope", T0)

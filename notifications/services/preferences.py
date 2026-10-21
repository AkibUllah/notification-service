"""Preference resolution: which channels a notification may use for this user, right now."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from notifications.core.channels import EXTERNAL_CHANNELS, Channel, DigestMode, Priority
from notifications.core.quiet_hours import in_quiet_hours, quiet_hours_end
from notifications.models import Preference, UnsubscribeToken, User
from notifications.services.errors import NotFound

CATCH_ALL = "*"


@dataclass(frozen=True)
class ChannelDecision:
    channel: Channel
    action: str  # send | skip | defer | digest
    reason: str | None = None
    deliver_at: datetime | None = None
    digest: DigestMode = DigestMode.NONE


@dataclass
class Resolution:
    decisions: list[ChannelDecision] = field(default_factory=list)

    def by_action(self, action: str) -> list[ChannelDecision]:
        return [d for d in self.decisions if d.action == action]


def get_preference(user: User, category: str) -> Preference | None:
    specific = next((p for p in user.preferences if p.category == category), None)
    return specific or next((p for p in user.preferences if p.category == CATCH_ALL), None)


def channel_allowed(user: User, category: str, channel: Channel) -> bool:
    for pref in (
        next((p for p in user.preferences if p.category == category), None),
        next((p for p in user.preferences if p.category == CATCH_ALL), None),
    ):
        if pref is not None and channel.value in pref.channels:
            return bool(pref.channels[channel.value])
    return True


def digest_mode(user: User, category: str) -> DigestMode:
    pref = get_preference(user, category)
    return DigestMode(pref.digest) if pref is not None else DigestMode.NONE


def set_preference(
    session: Session,
    user: User,
    category: str,
    *,
    channels: dict[str, bool] | None,
    digest: DigestMode | str | None,
    now: datetime,
) -> Preference:
    for name in channels or {}:
        Channel(name)  # validate
    pref = next((p for p in user.preferences if p.category == category), None)
    if pref is None:
        pref = Preference(category=category, channels={}, digest=DigestMode.NONE, updated_at=now)
        user.preferences.append(pref)
    if channels is not None:
        pref.channels = {**pref.channels, **channels}
    if digest is not None:
        pref.digest = DigestMode(digest)
    pref.updated_at = now
    session.flush()
    return pref


def resolve(
    user: User, category: str, requested: list[Channel], priority: Priority, now: datetime
) -> Resolution:
    """Decide per channel: send now, skip (opted out / no address), defer (quiet hours) or digest.

    High priority bypasses quiet hours and digests (think OTPs and fraud alerts). Quiet hours
    and digests only apply to interrupting channels (email, SMS, push).
    """
    resolution = Resolution()
    quiet = in_quiet_hours(now, user.quiet_hours_start, user.quiet_hours_end, user.timezone)
    mode = digest_mode(user, category)
    for channel in dict.fromkeys(requested):
        if not channel_allowed(user, category, channel):
            resolution.decisions.append(ChannelDecision(channel, "skip", "opted_out"))
            continue
        if not user.address_for(channel):
            resolution.decisions.append(ChannelDecision(channel, "skip", "no_address"))
            continue
        if channel in EXTERNAL_CHANNELS and priority is not Priority.HIGH:
            if mode is not DigestMode.NONE:
                resolution.decisions.append(
                    ChannelDecision(channel, "digest", f"digest_{mode.value}", digest=mode)
                )
                continue
            if quiet:
                resolution.decisions.append(
                    ChannelDecision(
                        channel,
                        "defer",
                        "quiet_hours",
                        deliver_at=quiet_hours_end(
                            now, user.quiet_hours_start, user.quiet_hours_end, user.timezone
                        ),
                    )
                )
                continue
        resolution.decisions.append(ChannelDecision(channel, "send"))
    return resolution


def issue_unsubscribe_token(
    session: Session, user: User, *, category: str | None, channel: Channel | None, now: datetime
) -> UnsubscribeToken:
    token = UnsubscribeToken(
        user_id=user.id,
        category=category,
        channel=channel.value if channel else None,
        created_at=now,
    )
    session.add(token)
    session.flush()
    return token


def unsubscribe(session: Session, token_value: str, now: datetime) -> UnsubscribeToken:
    token = session.get(UnsubscribeToken, token_value)
    if token is None:
        raise NotFound("unknown unsubscribe token")
    user = session.get(User, token.user_id)
    channels = (
        {token.channel: False} if token.channel else {c.value: False for c in EXTERNAL_CHANNELS}
    )
    set_preference(
        session, user, token.category or CATCH_ALL, channels=channels, digest=None, now=now
    )
    token.used_at = now
    session.flush()
    return token


def list_tokens(session: Session, user: User) -> list[UnsubscribeToken]:
    return list(
        session.scalars(select(UnsubscribeToken).where(UnsubscribeToken.user_id == user.id))
    )

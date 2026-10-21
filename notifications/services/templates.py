"""Versioned, per-locale Jinja2 templates with strict variable contracts.

A template declares the variables it uses. Saving fails if the body references an undeclared
variable; rendering fails if a declared variable is missing or an undeclared one is passed.
Built-ins (`user`, `unsubscribe_url`, `now`) are always available.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jinja2 import StrictUndefined, TemplateSyntaxError, meta
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from notifications.core.channels import LOCALES, Channel
from notifications.models import Template, Tenant, User
from notifications.services.errors import DomainError, NotFound

BUILTIN_VARIABLES = frozenset({"user", "unsubscribe_url", "now"})

_env = SandboxedEnvironment(
    undefined=StrictUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=True
)


@dataclass(frozen=True)
class Rendered:
    subject: str | None
    body: str
    template_id: str
    version: int
    locale: str


def referenced_variables(*sources: str | None) -> set[str]:
    names: set[str] = set()
    for source in sources:
        if not source:
            continue
        try:
            names |= meta.find_undeclared_variables(_env.parse(source))
        except TemplateSyntaxError as exc:
            raise DomainError(f"template syntax error: {exc}", code="template_syntax") from exc
    return names


def create_template(
    session: Session,
    tenant: Tenant,
    *,
    key: str,
    channel: Channel | str,
    locale: str,
    body: str,
    now: datetime,
    subject: str | None = None,
    variables: list[str] | None = None,
    created_by: str | None = None,
) -> Template:
    channel = Channel(channel)
    if locale not in LOCALES:
        raise DomainError(f"locale must be one of {LOCALES}", code="invalid_locale")
    if channel in (Channel.EMAIL, Channel.PUSH, Channel.IN_APP) and not subject:
        raise DomainError(
            f"{channel.value} templates need a subject/title", code="subject_required"
        )
    declared = set(variables or [])
    used = referenced_variables(subject, body) - BUILTIN_VARIABLES
    undeclared = used - declared
    if undeclared:
        raise DomainError(
            f"template uses undeclared variables: {sorted(undeclared)}", code="undeclared_variables"
        )
    latest = session.scalar(
        select(func.max(Template.version)).where(
            Template.tenant_id == tenant.id,
            Template.key == key,
            Template.channel == channel,
            Template.locale == locale,
        )
    )
    template = Template(
        tenant_id=tenant.id,
        key=key,
        channel=channel,
        locale=locale,
        version=(latest or 0) + 1,
        subject=subject,
        body=body,
        variables=sorted(declared),
        created_by=created_by,
        created_at=now,
    )
    session.add(template)
    session.flush()
    return template


def list_templates(session: Session, tenant: Tenant, key: str | None = None) -> list[Template]:
    stmt = select(Template).where(Template.tenant_id == tenant.id)
    if key:
        stmt = stmt.where(Template.key == key)
    return list(
        session.scalars(
            stmt.order_by(Template.key, Template.channel, Template.locale, Template.version)
        )
    )


def resolve(
    session: Session,
    tenant: Tenant,
    key: str,
    channel: Channel,
    locale: str,
    *,
    version: int | None = None,
    default_locale: str = "en",
) -> Template:
    """Latest active version for (key, channel, locale), falling back to the default locale."""
    for candidate in dict.fromkeys([locale, default_locale]):
        stmt = select(Template).where(
            Template.tenant_id == tenant.id,
            Template.key == key,
            Template.channel == channel,
            Template.locale == candidate,
            Template.active.is_(True),
        )
        if version is not None:
            stmt = stmt.where(Template.version == version)
        template = session.scalar(stmt.order_by(Template.version.desc()))
        if template is not None:
            return template
    raise NotFound(
        f"no {channel.value} template {key!r} for locale {locale!r} (or {default_locale!r})",
        code="template_not_found",
    )


def deactivate(session: Session, tenant: Tenant, template_id: str) -> Template:
    template = session.get(Template, template_id)
    if template is None or template.tenant_id != tenant.id:
        raise NotFound("template not found")
    template.active = False
    session.flush()
    return template


def render(
    template: Template,
    variables: dict[str, Any],
    *,
    user: User | None,
    now: datetime,
    unsubscribe_url: str | None,
) -> Rendered:
    declared = set(template.variables)
    missing = declared - set(variables)
    extra = set(variables) - declared
    if missing:
        raise DomainError(f"missing variables: {sorted(missing)}", code="missing_variables")
    if extra:
        raise DomainError(f"unexpected variables: {sorted(extra)}", code="unexpected_variables")
    context = dict(variables)
    context["user"] = (
        {
            "id": user.external_id,
            "name": user.name,
            "email": user.email,
            "phone": user.phone,
            "locale": user.locale,
        }
        if user
        else {}
    )
    context["unsubscribe_url"] = unsubscribe_url or ""
    context["now"] = now
    try:
        subject = (
            _env.from_string(template.subject).render(**context).strip()
            if template.subject
            else None
        )
        body = _env.from_string(template.body).render(**context)
    except Exception as exc:  # StrictUndefined, filters, etc.
        raise DomainError(f"render failed: {exc}", code="render_error") from exc
    return Rendered(subject, body, template.id, template.version, template.locale)

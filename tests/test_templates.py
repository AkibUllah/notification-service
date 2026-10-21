import pytest

from notifications.core.channels import Channel
from notifications.services import templates
from notifications.services.errors import DomainError, NotFound
from tests.conftest import T0, VARS


def test_saving_rejects_undeclared_variables_and_bad_syntax(session, tenant):
    with pytest.raises(DomainError, match="undeclared variables: \\['amount'\\]"):
        templates.create_template(
            session,
            tenant,
            key="x",
            channel="sms",
            locale="en",
            body="{{ amount }}",
            variables=[],
            now=T0,
        )
    with pytest.raises(DomainError, match="syntax error"):
        templates.create_template(
            session,
            tenant,
            key="x",
            channel="sms",
            locale="en",
            body="{% if %}",
            variables=[],
            now=T0,
        )
    with pytest.raises(DomainError, match="subject"):
        templates.create_template(
            session, tenant, key="x", channel="email", locale="en", body="hi", variables=[], now=T0
        )
    with pytest.raises(DomainError, match="locale"):
        templates.create_template(
            session, tenant, key="x", channel="sms", locale="fr", body="hi", variables=[], now=T0
        )
    ok = templates.create_template(
        session,
        tenant,
        key="x",
        channel="sms",
        locale="en",
        body="Hi {{ user.name }} {{ unsubscribe_url }}",
        variables=[],
        now=T0,
    )
    assert ok.variables == []  # built-ins need no declaration


def test_render_is_strict_about_variables(session, tenant, user, order_templates):
    tpl = order_templates["email"]
    rendered = templates.render(tpl, VARS, user=user, now=T0, unsubscribe_url="https://x/u/abc")
    assert rendered.subject == "Order ORD-4521 shipped"
    assert "Hi Akib, order ORD-4521 (৳1,500.00)" in rendered.body and rendered.body.endswith(
        "https://x/u/abc"
    )
    with pytest.raises(DomainError, match="missing variables: \\['amount'\\]"):
        templates.render(tpl, {"order_id": "1"}, user=user, now=T0, unsubscribe_url=None)
    with pytest.raises(DomainError, match="unexpected variables: \\['extra'\\]"):
        templates.render(tpl, {**VARS, "extra": 1}, user=user, now=T0, unsubscribe_url=None)


def test_render_with_undefined_attribute_fails_instead_of_blank(session, tenant):
    tpl = templates.create_template(
        session,
        tenant,
        key="x",
        channel="sms",
        locale="en",
        body="{{ user.nickname }}",
        variables=[],
        now=T0,
    )
    with pytest.raises(DomainError, match="render failed"):
        templates.render(tpl, {}, user=None, now=T0, unsubscribe_url=None)


def test_locale_fallback_and_versioning(session, tenant, user, order_templates):
    bn = templates.resolve(session, tenant, "order-shipped", Channel.EMAIL, "bn")
    assert bn.locale == "bn"
    sms_bn = templates.resolve(session, tenant, "order-shipped", Channel.SMS, "bn")
    assert sms_bn.locale == "en"  # no Bangla SMS variant: default locale
    v2 = templates.create_template(
        session,
        tenant,
        key="order-shipped",
        channel="sms",
        locale="en",
        body="v2 {{ order_id }}",
        variables=["order_id", "amount"],
        now=T0,
    )
    assert v2.version == 2
    assert templates.resolve(session, tenant, "order-shipped", Channel.SMS, "en").version == 2
    assert templates.resolve(
        session, tenant, "order-shipped", Channel.SMS, "en", version=1
    ).body.startswith("Order")
    templates.deactivate(session, tenant, v2.id)
    assert templates.resolve(session, tenant, "order-shipped", Channel.SMS, "en").version == 1
    with pytest.raises(NotFound):
        templates.resolve(session, tenant, "order-shipped", Channel.WEBHOOK, "bn", version=9)
    assert len(templates.list_templates(session, tenant, key="order-shipped")) == 7


def test_sandbox_blocks_dangerous_expressions(session, tenant):
    tpl = templates.create_template(
        session,
        tenant,
        key="x",
        channel="sms",
        locale="en",
        body="{{ ''.__class__.__mro__ }}",
        variables=[],
        now=T0,
    )
    with pytest.raises(DomainError, match="render failed"):
        templates.render(tpl, {}, user=None, now=T0, unsubscribe_url=None)

import json
from datetime import timedelta

from notifications.core.signing import HEADER, verify
from tests.conftest import ADMIN, T0, VARS


def setup(client):
    r = client.post(
        "/v1/tenants",
        json={"name": "Dhaka Grocers", "webhook_url": "https://shop.example.com/notify-status"},
        headers=ADMIN,
    )
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]
    h = {"X-API-Key": key}
    r = client.put(
        "/v1/users",
        json={
            "external_id": "u-42",
            "name": "Akib",
            "email": "akib@example.com",
            "phone": "+8801711000000",
            "locale": "en",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    for channel, subject, body in [
        (
            "email",
            "Order {{ order_id }} shipped",
            "Hi {{ user.name }}, {{ amount }}. {{ unsubscribe_url }}",
        ),
        ("sms", None, "Order {{ order_id }} shipped. {{ amount }}"),
        ("in_app", "Order {{ order_id }}", "{{ amount }}"),
    ]:
        r = client.post(
            "/v1/templates",
            json={
                "key": "order-shipped",
                "channel": channel,
                "locale": "en",
                "subject": subject,
                "body": body,
                "variables": ["order_id", "amount"],
            },
            headers=h,
        )
        assert r.status_code == 201, r.text
    return h, r.json()


def test_health_auth_and_tenant_listing(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["worker"] == "disabled" and len(h["providers"]) == 4
    assert client.get("/v1/tenants").status_code == 401
    assert client.get("/v1/users").status_code == 401
    assert client.get("/v1/users", headers={"X-API-Key": "ntk-wrong"}).status_code == 401
    setup(client)
    tenants = client.get("/v1/tenants", headers=ADMIN).json()
    assert (
        len(tenants) == 1
        and "api_key" not in tenants[0]
        and tenants[0]["api_key_prefix"].startswith("ntk-")
    )


def test_users_preferences_and_unsubscribe_link(client):
    h, _ = setup(client)
    r = client.put(
        "/v1/users",
        json={
            "external_id": "u-42",
            "quiet_hours_start": "22:00",
            "quiet_hours_end": "08:00",
            "push_token": "tok",
        },
        headers=h,
    )
    assert (
        r.json()["quiet_hours_start"] == "22:00" and r.json()["email"] == "akib@example.com"
    )  # upsert keeps fields
    assert (
        client.put(
            "/v1/users", json={"external_id": "x", "quiet_hours_start": "22:00"}, headers=h
        ).status_code
        == 409
    )
    assert (
        client.put("/v1/users", json={"external_id": "x", "phone": "abc"}, headers=h).status_code
        == 422
    )
    r = client.put(
        "/v1/users/u-42/preferences",
        json={"category": "marketing", "channels": {"sms": False}, "digest": "daily"},
        headers=h,
    )
    assert r.json()[0] == {
        "category": "marketing",
        "channels": {"sms": False},
        "digest": "daily",
        "updated_at": r.json()[0]["updated_at"],
    }
    assert client.get("/v1/users/u-42", headers=h).json()["preferences"][0]["digest"] == "daily"
    assert client.get("/v1/users/ghost", headers=h).status_code == 404
    r = client.post("/v1/users/u-42/unsubscribe-token?category=marketing&channel=email", headers=h)
    assert r.json()["url"].startswith("https://notify.example.com/v1/unsubscribe/")
    page = client.get(f"/v1/unsubscribe/{r.json()['token']}")
    assert page.status_code == 200 and "unsubscribed" in page.text.lower()
    prefs = client.get("/v1/users/u-42", headers=h).json()["preferences"][0]
    assert prefs["channels"] == {"sms": False, "email": False}
    assert client.get("/v1/unsubscribe/nope").status_code == 404


def test_templates_crud_and_preview(client):
    h, tpl = setup(client)
    r = client.post(
        "/v1/templates",
        json={
            "key": "order-shipped",
            "channel": "sms",
            "locale": "en",
            "body": "{{ nope }}",
            "variables": [],
        },
        headers=h,
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "undeclared_variables"
    r = client.post(
        "/v1/templates/render",
        json={
            "key": "order-shipped",
            "channel": "email",
            "locale": "bn",
            "variables": VARS,
            "user_external_id": "u-42",
        },
        headers=h,
    )
    assert (
        r.status_code == 200
        and r.json()["locale"] == "en"
        and r.json()["subject"] == "Order ORD-4521 shipped"
        and "Hi Akib" in r.json()["body"]
    )
    r = client.post(
        "/v1/templates/render",
        json={"key": "order-shipped", "channel": "email", "variables": {"order_id": "1"}},
        headers=h,
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "missing_variables"
    r = client.post(
        "/v1/templates",
        json={
            "key": "order-shipped",
            "channel": "sms",
            "locale": "en",
            "body": "v2 {{ order_id }} {{ amount }}",
            "variables": ["order_id", "amount"],
        },
        headers=h,
    )
    assert r.json()["version"] == 2
    assert len(client.get("/v1/templates?key=order-shipped", headers=h).json()) == 4
    assert client.delete(f"/v1/templates/{r.json()['id']}", headers=h).json()["active"] is False
    assert (
        client.post(
            "/v1/templates/render",
            json={"key": "order-shipped", "channel": "sms", "variables": VARS},
            headers=h,
        ).json()["version"]
        == 1
    )


def test_notification_flow_idempotency_inbox_and_worker(client, fakes):
    h, _ = setup(client)
    body = {
        "user": "u-42",
        "category": "orders",
        "template": "order-shipped",
        "variables": VARS,
        "idempotency_key": "order-4521-shipped",
        "channels": ["email", "sms", "in_app"],
    }
    r = client.post("/v1/notifications", json=body, headers=h)
    assert r.status_code == 202, r.text
    n = r.json()
    assert n["status"] == "queued" and {d["channel"] for d in n["deliveries"]} == {
        "email",
        "sms",
        "in_app",
    }
    replay = client.post("/v1/notifications", json=body, headers=h)
    assert (
        replay.status_code == 200
        and replay.headers["X-Idempotent-Replay"] == "true"
        and replay.json()["id"] == n["id"]
    )
    assert client.get("/v1/notifications", headers=h).headers["X-Total-Count"] == "1"

    run = client.post("/v1/worker/run-once", headers=ADMIN).json()
    assert run["sent"] == 3 and run["digests_flushed"] == 0
    got = client.get(f"/v1/notifications/{n['id']}", headers=h).json()
    assert got["status"] == "sent" and all(d["status"] == "sent" for d in got["deliveries"])
    assert fakes.email.sent[0].to == "akib@example.com"
    assert (
        client.get("/v1/deliveries?status=sent&channel=email", headers=h).json()[0]["provider"]
        == "email_primary"
    )

    inbox = client.get("/v1/inbox/u-42?unread=true", headers=h).json()
    assert (
        len(inbox) == 1
        and inbox[0]["title"] == "Order ORD-4521"
        and inbox[0]["body"] == "৳1,500.00"
    )
    assert client.get("/v1/inbox/u-42/unread-count", headers=h).json() == {"unread_count": 1}
    r = client.post("/v1/inbox/u-42/read", json={"ids": [inbox[0]["id"]]}, headers=h)
    assert r.json() == {"marked": 1, "unread_count": 0}
    assert client.get("/v1/inbox/u-42?unread=true", headers=h).json() == []

    r = client.post(
        "/v1/receipts",
        json={"provider_message_id": "email_primary-1", "status": "delivered"},
        headers=h,
    )
    assert r.json()["receipt_status"] == "delivered"
    assert (
        client.post(
            "/v1/receipts", json={"provider_message_id": "nope", "status": "delivered"}, headers=h
        ).status_code
        == 404
    )


def test_bulk_partial_failure_and_rate_limit(client):
    h, _ = setup(client)
    items = [
        {
            "user": "u-42",
            "category": "orders",
            "template": "order-shipped",
            "variables": VARS,
            "channels": ["sms"],
        },
        {"user": "ghost", "category": "orders", "template": "order-shipped", "variables": VARS},
        {
            "user": "u-42",
            "category": "orders",
            "template": "missing",
            "variables": VARS,
            "channels": ["sms"],
        },
    ]
    r = client.post("/v1/notifications/bulk", json={"notifications": items}, headers=h)
    assert r.status_code == 202
    assert len(r.json()["accepted"]) == 1
    assert [(e["index"], e["code"]) for e in r.json()["errors"]] == [
        (1, "not_found"),
        (2, "template_not_found"),
    ]
    tenant = client.get("/v1/tenants", headers=ADMIN).json()[0]
    assert tenant["rate_limit_per_minute"] == 600
    r = client.post(
        "/v1/tenants", json={"name": "Tiny", "rate_limit_per_minute": 1}, headers=ADMIN
    ).json()
    tiny = {"X-API-Key": r["api_key"]}
    client.put("/v1/users", json={"external_id": "t1", "phone": "+8801711000001"}, headers=tiny)
    client.post(
        "/v1/templates",
        json={"key": "ping", "channel": "sms", "locale": "en", "body": "ping", "variables": []},
        headers=tiny,
    )
    assert (
        client.post(
            "/v1/notifications",
            json={"user": "t1", "category": "c", "template": "ping", "channels": ["sms"]},
            headers=tiny,
        ).status_code
        == 202
    )
    limited = client.post(
        "/v1/notifications",
        json={"user": "t1", "category": "c", "template": "ping", "channels": ["sms"]},
        headers=tiny,
    )
    assert (
        limited.status_code == 429
        and limited.headers["Retry-After"] == "60"
        and limited.json()["error"]["code"] == "rate_limited"
    )


def test_dead_letters_replay_and_provider_health(client, fakes, clock):
    h, _ = setup(client)
    fakes.sms.always_fail = True
    client.post(
        "/v1/notifications",
        json={
            "user": "u-42",
            "category": "orders",
            "template": "order-shipped",
            "variables": VARS,
            "channels": ["sms"],
        },
        headers=h,
    )
    for seconds in (0, 30, 150):
        clock.set(T0 + timedelta(seconds=seconds))
        client.post("/v1/worker/run-once", headers=ADMIN)
    dead = client.get("/v1/dead-letters?channel=sms", headers=ADMIN).json()
    assert len(dead) == 1 and dead[0]["attempts"] == 3
    providers = {p["channel"]: p for p in client.get("/v1/providers", headers=ADMIN).json()}
    assert (
        providers["sms"]["providers"][0]["healthy"] is False
        and providers["sms"]["providers"][0]["consecutive_failures"] == 3
    )
    fakes.sms.always_fail = False
    assert (
        client.post(f"/v1/deliveries/{dead[0]['id']}/replay", headers=ADMIN).json()["status"]
        == "pending"
    )
    clock.advance(minutes=5)
    assert client.post("/v1/worker/run-once", headers=ADMIN).json()["sent"] == 1
    assert client.get("/v1/dead-letters", headers=ADMIN).json() == []
    assert client.post("/v1/deliveries/dlv_nope/replay", headers=ADMIN).status_code == 404


def test_inbox_sse_stream(client):
    h, _ = setup(client)
    client.post(
        "/v1/notifications",
        json={
            "user": "u-42",
            "category": "orders",
            "template": "order-shipped",
            "variables": VARS,
            "channels": ["in_app"],
        },
        headers=h,
    )
    client.post("/v1/worker/run-once", headers=ADMIN)
    with client.stream("GET", "/v1/inbox/u-42/stream", params={"max_events": 1}, headers=h) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        text = "".join(r.iter_text())
    assert text.startswith("event: ready\n")
    assert json.loads(text.split("data: ", 1)[1].strip()) == {"unread_count": 1, "user": "u-42"}


def test_status_webhook_is_signed(client, receiver):
    h, _ = setup(client)
    secret = client.get("/v1/tenants", headers=ADMIN).json()[0]
    created = client.post(
        "/v1/tenants",
        json={
            "name": "Signed",
            "webhook_url": "https://shop.example.com/notify-status",
            "webhook_secret": "s3cr3t-s3cr3t-s3cr3t",
        },
        headers=ADMIN,
    ).json()
    assert created["webhook_secret"] == "s3cr3t-s3cr3t-s3cr3t" and secret["name"] == "Dhaka Grocers"
    sh = {"X-API-Key": created["api_key"]}
    client.put("/v1/users", json={"external_id": "s1", "phone": "+8801711000002"}, headers=sh)
    client.post(
        "/v1/templates",
        json={"key": "ping", "channel": "sms", "locale": "en", "body": "ping", "variables": []},
        headers=sh,
    )
    client.post(
        "/v1/notifications",
        json={"user": "s1", "category": "c", "template": "ping", "channels": ["sms"]},
        headers=sh,
    )
    client.post("/v1/worker/run-once", headers=ADMIN)
    req = [r for r in receiver.requests if r.url.path == "/notify-status"][-1]
    verify("s3cr3t-s3cr3t-s3cr3t", req.content, req.headers[HEADER], now=int(T0.timestamp()))
    assert (
        req.headers["X-Notify-Event"] == "delivery.sent"
        and json.loads(req.content)["data"]["channel"] == "sms"
    )

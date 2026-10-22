#!/usr/bin/env bash
# Codespaces start hook: bring up API + worker + PostgreSQL, then seed a tenant, a user,
# an in-app template and send one demo in-app notification.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose up -d --build

echo "waiting for the API on :8000 ..."
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

BASE=http://127.0.0.1:8000/v1
ADMIN_KEY=${ADMIN_API_KEY:-change-me}   # docker-compose.yml default
if [ ! -f .demo-tenant.txt ]; then
  TENANT_KEY=$(curl -fsS -X POST "$BASE/tenants" -H "X-API-Key: $ADMIN_KEY" \
    -H 'Content-Type: application/json' -d '{"name":"Dhaka Grocers"}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')
  T=(-H "X-API-Key: $TENANT_KEY" -H 'Content-Type: application/json')
  curl -fsS -X PUT "$BASE/users" "${T[@]}" -d '{"external_id":"u-42","name":"Akib",
    "email":"akib@example.com","phone":"+8801711000000","locale":"en"}' >/dev/null
  curl -fsS -X POST "$BASE/templates" "${T[@]}" -d '{"key":"order-shipped","channel":"in_app",
    "locale":"en","subject":"Order {{ order_id }} shipped","body":"{{ amount }} · tap to track",
    "variables":["order_id","amount"]}' >/dev/null
  curl -fsS -X POST "$BASE/notifications" "${T[@]}" -d '{"user":"u-42","category":"orders",
    "template":"order-shipped","variables":{"order_id":"ORD-4521","amount":"৳1,500.00"},
    "channels":["in_app"],"idempotency_key":"codespace-demo-order-4521"}' >/dev/null
  echo "tenant_api_key: $TENANT_KEY" > .demo-tenant.txt
fi

cat <<MSG

notification-service is up (the worker container delivers queued notifications every 2 s).
  API docs:    forwarded port 8000, path /docs   (admin X-API-Key: $ADMIN_KEY)
  Tenant key:  .demo-tenant.txt  (use as X-API-Key for /v1/users, /v1/templates, /v1/notifications)
  Inbox:       GET /v1/inbox/u-42 and /v1/inbox/u-42/stream (SSE) with the tenant key
MSG

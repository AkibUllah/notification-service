# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- CodeQL analysis for Python and GitHub Actions on pushes and pull requests to `main` and on a weekly schedule.
- Dependabot version updates for pip, Docker and GitHub Actions, with minor and patch updates grouped per ecosystem.
- README screenshots: a send session against a local API and the API reference.
- Roadmap items tracked as GitHub issues under the v0.2.0 milestone.

### Changed

- CI workflow runs with a read-only `GITHUB_TOKEN`.
- Test count in the README updated to 55.

## [0.1.0] - 2026-10-25

First public release of notification-service.

### Added

- Channels: email, SMS, push, in-app inbox and user webhooks, each with a `Provider` protocol; console email/SMS and a JSON-lines push file for development, clearly labelled stubs for SMTP (`aiosmtplib`), a Bangladeshi SMS aggregator (HTTP) and FCM v1
- Fallback chains per channel with health tracking: a provider that fails N times in a row is skipped for a cooldown, then probed again; the chain never drops a message just because every provider is cooling down
- Templates: versioned per `(key, channel, locale)`, en/bn with default-locale fallback, sandboxed Jinja2 with `StrictUndefined`; the template declares its variables and saving rejects undeclared references, rendering rejects missing or unexpected ones; built-ins `user`, `unsubscribe_url`, `now`; preview endpoint
- Preferences per category with a `*` catch-all: channel opt-in/out, quiet hours in the user's timezone (overnight windows handled), hourly/daily digests; high priority bypasses quiet hours and digests (OTPs, fraud alerts); one-click unsubscribe tokens, scoped to a category and/or channel
- Intake: single and bulk `POST /v1/notifications`, idempotency keys (replay returns the original with `200` + `X-Idempotent-Replay`), sliding-window rate limit per tenant with `Retry-After`, template resolution at accept time so a typo fails the request, not the worker
- Worker: due deliveries by priority then time, retries at 30s/2m/10m/30m (configurable), dead letters after `MAX_ATTEMPTS`, non-retryable failures (bad address, unregistered push token, render error) go straight to dead letters; replay endpoint; runs in-process or as `python -m notifications.worker`
- Digest scheduler groups batched notifications per user, category and channel and flushes them at the top of the hour or at 09:00 local, using the tenant's `digest` template or a built-in bilingual list
- Delivery status: HMAC-signed `delivery.sent` / `delivery.dead` webhooks to the tenant; inbound receipts (`delivered`, `bounced`, `read`, …) matched by provider message id
- In-app inbox: list, unread count, mark read, and `GET /v1/inbox/{user}/stream` SSE with `ready`, `message` and `read` events
- Multi-tenant with hashed API keys; admin API for tenants, provider health, dead letters and manual worker runs; SQLite for dev/tests, PostgreSQL via docker compose

[Unreleased]: https://github.com/AkibUllah/notification-service/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/AkibUllah/notification-service/releases/tag/v0.1.0

"""HTTP-based providers: the webhook channel and the clearly-labelled vendor stubs."""

from __future__ import annotations

import json

import httpx

from notifications.core.channels import Channel
from notifications.providers.base import Message, ProviderNotConfigured, ProviderResult


class WebhookProvider:
    """The `webhook` channel: POST the rendered notification to the user's webhook URL."""

    name = "http_webhook"
    channel = Channel.WEBHOOK

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def send(self, message: Message) -> ProviderResult:
        payload = {
            "notification_id": message.notification_id,
            "delivery_id": message.delivery_id,
            "subject": message.subject,
            "body": message.body,
            "data": message.data,
        }
        try:
            response = await self.http.post(
                message.to, json=payload, headers={"X-Notify-Event": "notification"}
            )
        except httpx.HTTPError as exc:
            return ProviderResult(False, error=f"{type(exc).__name__}: {exc}")
        if response.is_success:
            return ProviderResult(True, provider_message_id=f"http-{response.status_code}")
        return ProviderResult(
            False,
            error=f"HTTP {response.status_code}",
            retryable=response.status_code >= 500 or response.status_code == 429,
        )


class BdSmsAggregatorProvider:
    """STUB: shaped like the HTTP APIs of Bangladeshi SMS aggregators
    (api_key, senderid, number, message).

    This is not an integration with any specific vendor. It only talks to `BD_SMS_API_URL`.
    """

    name = "bd_sms"
    channel = Channel.SMS

    def __init__(
        self,
        http: httpx.AsyncClient,
        api_url: str | None,
        api_key: str | None,
        sender_id: str | None,
    ) -> None:
        self.http = http
        self.api_url = api_url
        self.api_key = api_key
        self.sender_id = sender_id

    async def send(self, message: Message) -> ProviderResult:
        if not (self.api_url and self.api_key and self.sender_id):
            raise ProviderNotConfigured(
                "BD_SMS_API_URL, BD_SMS_API_KEY and BD_SMS_SENDER_ID are required"
            )
        try:
            response = await self.http.post(
                self.api_url,
                data={
                    "api_key": self.api_key,
                    "senderid": self.sender_id,
                    "number": message.to,
                    "message": message.body,
                    "type": "unicode",
                },
            )
        except httpx.HTTPError as exc:
            return ProviderResult(False, error=f"{type(exc).__name__}: {exc}")
        body = response.json() if response.content else {}
        if response.is_success and str(body.get("response_code", "")) in ("202", "200", "SUCCESS"):
            return ProviderResult(
                True,
                provider_message_id=str(
                    body.get("message_id") or body.get("success_message", "ok")
                ),
            )
        return ProviderResult(
            False,
            error=str(body.get("error_message") or f"HTTP {response.status_code}"),
            retryable=response.status_code >= 500,
        )


class FcmProvider:
    """STUB: Firebase Cloud Messaging HTTP v1 request shape. Not wired to Google auth."""

    name = "fcm"
    channel = Channel.PUSH

    def __init__(
        self, http: httpx.AsyncClient, project_id: str | None, access_token: str | None
    ) -> None:
        self.http = http
        self.project_id = project_id
        self.access_token = access_token

    async def send(self, message: Message) -> ProviderResult:
        if not (self.project_id and self.access_token):
            raise ProviderNotConfigured("FCM_PROJECT_ID and FCM_ACCESS_TOKEN are required")
        url = f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"
        payload = {
            "message": {
                "token": message.to,
                "notification": {"title": message.subject or "", "body": message.body},
                "data": {k: str(v) for k, v in message.data.items()},
            }
        }
        try:
            response = await self.http.post(
                url, json=payload, headers={"Authorization": f"Bearer {self.access_token}"}
            )
        except httpx.HTTPError as exc:
            return ProviderResult(False, error=f"{type(exc).__name__}: {exc}")
        if response.is_success:
            return ProviderResult(True, provider_message_id=response.json().get("name"))
        error = response.json().get("error", {}) if response.content else {}
        status = error.get("status", f"HTTP {response.status_code}")
        # UNREGISTERED tokens will never work again: do not retry.
        return ProviderResult(
            False,
            error=json.dumps(error) if error else status,
            retryable=status not in ("UNREGISTERED", "INVALID_ARGUMENT"),
        )

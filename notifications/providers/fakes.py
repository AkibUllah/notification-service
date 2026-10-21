"""Development providers: print to the log, keep an in-memory record, or append to a file."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from notifications.core.channels import Channel
from notifications.providers.base import Message, ProviderResult

log = logging.getLogger(__name__)


class ConsoleEmailProvider:
    name = "console_email"
    channel = Channel.EMAIL

    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def send(self, message: Message) -> ProviderResult:
        self.sent.append(message)
        log.info("EMAIL to=%s subject=%r\n%s", message.to, message.subject, message.body)
        return ProviderResult(True, provider_message_id=f"console-email-{len(self.sent)}")


class ConsoleSMSProvider:
    name = "console_sms"
    channel = Channel.SMS

    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def send(self, message: Message) -> ProviderResult:
        self.sent.append(message)
        log.info("SMS to=%s: %s", message.to, message.body)
        return ProviderResult(True, provider_message_id=f"console-sms-{len(self.sent)}")


class FilePushProvider:
    """Appends each push as a JSON line, so a local app build can tail the file."""

    name = "file_push"
    channel = Channel.PUSH

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.count = 0

    async def send(self, message: Message) -> ProviderResult:
        self.count += 1
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "token": message.to,
            "title": message.subject,
            "body": message.body,
            "data": message.data,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return ProviderResult(True, provider_message_id=f"file-push-{self.count}")


class FakeProvider:
    """Scriptable provider for tests: fails the first `fail_times` sends, or always."""

    def __init__(
        self,
        channel: Channel,
        name: str = "fake",
        *,
        fail_times: int = 0,
        always_fail: bool = False,
        retryable: bool = True,
    ) -> None:
        self.channel = channel
        self.name = name
        self.fail_times = fail_times
        self.always_fail = always_fail
        self.retryable = retryable
        self.sent: list[Message] = []
        self.calls = 0

    async def send(self, message: Message) -> ProviderResult:
        self.calls += 1
        if self.always_fail or self.calls <= self.fail_times:
            return ProviderResult(False, error=f"{self.name} unavailable", retryable=self.retryable)
        self.sent.append(message)
        return ProviderResult(True, provider_message_id=f"{self.name}-{len(self.sent)}")

"""STUB: SMTP delivery via aiosmtplib (optional extra). Labelled as a stub because no mail
server is part of this repository; set SMTP_HOST to point it at Mailpit, Postfix or a relay."""

from __future__ import annotations

from email.message import EmailMessage

from notifications.core.channels import Channel
from notifications.providers.base import Message, ProviderNotConfigured, ProviderResult


class SmtpProvider:
    name = "smtp"
    channel = Channel.EMAIL

    def __init__(
        self, host: str | None, port: int, username: str | None, password: str | None, sender: str
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender

    def build(self, message: Message) -> EmailMessage:
        email = EmailMessage()
        email["From"] = self.sender
        email["To"] = message.to
        email["Subject"] = message.subject or ""
        if message.body.lstrip().startswith("<"):
            email.set_content("This message requires an HTML capable client.")
            email.add_alternative(message.body, subtype="html")
        else:
            email.set_content(message.body)
        return email

    async def send(self, message: Message) -> ProviderResult:
        if not self.host:
            raise ProviderNotConfigured("SMTP_HOST is required")
        try:
            import aiosmtplib
        except ImportError as exc:  # pragma: no cover
            raise ProviderNotConfigured("install the smtp extra: pip install -e '.[smtp]'") from exc
        try:
            response = await aiosmtplib.send(
                self.build(message),
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                start_tls=self.port == 587,
                timeout=10,
            )
        except (aiosmtplib.SMTPException, OSError) as exc:
            return ProviderResult(False, error=f"{type(exc).__name__}: {exc}")
        return ProviderResult(
            True, provider_message_id=str(response[1]) if isinstance(response, tuple) else None
        )

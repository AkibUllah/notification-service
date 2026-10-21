"""HMAC-SHA256 signatures for outgoing status webhooks."""

from __future__ import annotations

import hashlib
import hmac

HEADER = "X-Notify-Signature"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


class SignatureError(ValueError):
    pass


def verify(secret: str, body: bytes, header: str, now: int, tolerance_seconds: int = 300) -> None:
    try:
        parts = dict(part.split("=", 1) for part in header.split(","))
        timestamp = int(parts["t"])
        provided = parts["v1"]
    except (ValueError, KeyError) as exc:
        raise SignatureError("malformed signature header") from exc
    if abs(now - timestamp) > tolerance_seconds:
        raise SignatureError("timestamp outside tolerance")
    expected = sign(secret, timestamp, body).split("v1=", 1)[1]
    if not hmac.compare_digest(expected, provided):
        raise SignatureError("signature mismatch")

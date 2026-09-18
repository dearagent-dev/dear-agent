from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

SIGNATURE_HEADER = "X-Herald-Signature"


def utcnow() -> datetime:
    return datetime.now(UTC)


class AuthError(Exception):
    """Base class for inbound authentication errors."""


class MissingSignatureError(AuthError):
    """The message carried no signature header."""


class UnauthorizedSenderError(AuthError):
    """The sender is not on the allowlist."""


class BadSignatureError(AuthError):
    """The signature did not match the shared secret."""


class RateLimitedError(AuthError):
    """The sender exceeded the allowed message rate."""


def sign(body: str, secret: str, *, sender: str | None = None) -> str:
    """Return the ``sha256=<hex>`` HMAC of ``body`` (and sender) under ``secret``.

    Including the sender in the signed material prevents a valid signature from being
    replayed under a different ``From``.
    """
    message = body if sender is None else f"{sender}\n{body}"
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@dataclass(slots=True)
class InboundAuthorizer:
    """Authorizes inbound mail by shared secret and an optional sender allowlist.

    The ``From`` header is advisory: authorization comes from the HMAC signature, never
    from the address alone. An empty allowlist means "any sender with a valid signature".
    """

    secret: str
    allowlist: frozenset[str] = field(default_factory=frozenset)

    def authorize(self, *, body: str, headers: dict[str, str], sender: str | None) -> None:
        if self.allowlist and sender not in self.allowlist:
            raise UnauthorizedSenderError(sender)

        signature = headers.get(SIGNATURE_HEADER)
        if not signature:
            raise MissingSignatureError("missing signature header")

        expected = sign(body, self.secret, sender=sender)
        if not hmac.compare_digest(signature, expected):
            raise BadSignatureError("signature mismatch")


class RateLimiter:
    """Sliding-window rate limiter, per sender.

    A burst of messages from one sender must not become a burst of agent runs. The clock
    is injectable so tests do not sleep.
    """

    def __init__(
        self,
        *,
        limit: int,
        window: timedelta,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        self._limit = limit
        self._window = window
        self._clock = clock
        self._seen: dict[str, list[datetime]] = {}

    def check(self, sender: str | None) -> None:
        key = sender or "<unknown>"
        now = self._clock()
        cutoff = now - self._window
        recent = [moment for moment in self._seen.get(key, []) if moment > cutoff]
        if len(recent) >= self._limit:
            self._seen[key] = recent
            raise RateLimitedError(f"{key} exceeded {self._limit} messages per {self._window}")
        recent.append(now)
        self._seen[key] = recent


@dataclass(slots=True)
class InboundGate:
    """Combines authorization and rate limiting before normalization."""

    authorizer: InboundAuthorizer
    rate_limiter: RateLimiter

    def admit(self, *, body: str, headers: dict[str, str], sender: str | None) -> None:
        self.authorizer.authorize(body=body, headers=headers, sender=sender)
        self.rate_limiter.check(sender)


__all__ = [
    "AuthError",
    "BadSignatureError",
    "InboundAuthorizer",
    "InboundGate",
    "MissingSignatureError",
    "RateLimitedError",
    "RateLimiter",
    "SIGNATURE_HEADER",
    "UnauthorizedSenderError",
    "sign",
]

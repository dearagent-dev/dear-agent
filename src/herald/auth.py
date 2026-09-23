from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

SIGNATURE_HEADER = "X-Herald-Signature"

# The MTA whose Authentication-Results we trust (Fastmail's is *.messagingengine.com).
DEFAULT_AUTH_SERV_ID = "messagingengine.com"


@runtime_checkable
class Gate(Protocol):
    """Anything that can admit or reject an inbound message before normalization."""

    def admit(self, *, body: str, headers: dict[str, str], sender: str | None) -> None: ...


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


class EmailAuthError(AuthError):
    """The receiving MTA's SPF/DKIM/DMARC verdict did not pass."""


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


@dataclass(slots=True)
class SenderAllowlist:
    """Admits inbound mail only from allowed senders, with no signature.

    The email transport cannot carry Herald's HMAC header, so this is the first line of
    defence: entries are exact addresses (``me@example.com``) or domains (``@example.com``).
    ``From`` is spoofable, so this is not a boundary — pair it with a shared secret or a
    DMARC check later (see ``docs/security.md``). An empty list rejects everyone, so a
    misconfigured deployment fails closed rather than open.
    """

    senders: frozenset[str]

    @classmethod
    def from_env(cls, value: str | None) -> SenderAllowlist | None:
        if not value:
            return None
        entries = frozenset(
            part.strip().lower() for part in value.replace(";", ",").split(",") if part.strip()
        )
        return cls(senders=entries) if entries else None

    def admit(self, *, body: str, headers: dict[str, str], sender: str | None) -> None:
        candidate = (sender or "").strip().lower()
        if candidate:
            for allowed in self.senders:
                if candidate == allowed or (
                    allowed.startswith("@") and candidate.endswith(allowed)
                ):
                    return
        raise UnauthorizedSenderError(sender)


_AUTH_MECHANISMS = ("dmarc", "dkim", "spf", "arc")


def _domain(address: str | None) -> str:
    return (address or "").rsplit("@", 1)[-1].strip().lower()


def _authentication_results(headers: Mapping[str, str] | None) -> str | None:
    values = [
        value
        for name, value in (headers or {}).items()
        if name.lower() == "authentication-results" and value
    ]
    return "\n".join(values) if values else None


# A new ``Authentication-Results`` field starts with its authserv-id followed by ``;``; a
# folded continuation line starts with a ``mechanism=result`` clause (an ``=`` before any
# ``;``). That distinction lets us separate the MTA's field from one an attacker appended.
_AUTHSERV_LINE_RE = re.compile(r"^\s*[A-Za-z0-9._-]+(?:\s+\([^)]*\))?\s*;")


def _auth_results_blocks(raw: str) -> list[str]:
    """Split concatenated ``Authentication-Results`` values into individual header fields."""
    blocks: list[str] = []
    current: list[str] = []
    for line in raw.splitlines():
        if _AUTHSERV_LINE_RE.match(line):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
        elif line.strip():
            current = [line]
    if current:
        blocks.append("\n".join(current))
    return blocks


def _authserv_id(block: str) -> str:
    head = block.split(";", 1)[0].strip()
    return head.split()[0] if head else ""


def _authserv_id_matches(serv_id: str, configured: str) -> bool:
    """True when ``serv_id`` is ``configured`` or a subdomain of it (a domain boundary).

    A substring check would accept ``notmessagingengine.com``; a suffix check does not.
    """
    serv_id = serv_id.strip().lower().rstrip(".")
    configured = configured.strip().lower().rstrip(".")
    return bool(configured) and (serv_id == configured or serv_id.endswith("." + configured))


def _trusted_authentication_results(raw: str, auth_serv_id: str) -> str | None:
    """Keep only the fields written by the configured receiving MTA.

    An attacker can inject their own ``Authentication-Results`` header; it is not from the
    trusted authserv-id, so it is discarded. Fields with no trusted authserv-id at all fail
    closed (return ``None``).
    """
    blocks = _auth_results_blocks(raw)
    if not auth_serv_id:
        return "\n".join(blocks) if blocks else None
    trusted = [block for block in blocks if _authserv_id_matches(_authserv_id(block), auth_serv_id)]
    return "\n".join(trusted) if trusted else None


def parse_authentication_results(raw: str) -> dict[str, str]:
    """Extract the verdicts and authenticated domains from ``Authentication-Results``.

    RFC 8601 allows several fields and folded values, so we scan every clause for the
    mechanisms and for the ``header.from``/``header.d``/``smtp.mailfrom`` properties. The
    **first** verdict for a mechanism (and the first authenticated domain) wins: the
    outermost field is the MTA's, so a later clause can never upgrade a ``fail`` to a ``pass``.
    """
    verdicts: dict[str, str] = {}
    for clause in re.split(r"[;\n]", raw):
        clause = clause.strip()
        match = re.match(r"([A-Za-z0-9-]+)\s*=\s*([A-Za-z0-9-]+)", clause)
        if match:
            mechanism, verdict = match.group(1).lower(), match.group(2).lower()
            if mechanism in _AUTH_MECHANISMS:
                verdicts.setdefault(mechanism, verdict)
        for prop in re.finditer(
            r"(header\.from|header\.d|smtp\.mailfrom)\s*=\s*([^\s;)]+)", clause, re.IGNORECASE
        ):
            key, value = prop.group(1).lower(), prop.group(2).strip("<>").lower()
            if key == "header.from":
                verdicts.setdefault("dmarc_from", _domain(value))
            elif key == "header.d":
                verdicts.setdefault("dkim_domain", value.lstrip("@"))
            else:
                verdicts.setdefault("spf_domain", _domain(value))
    return verdicts


@dataclass(slots=True)
class EmailAuthGate:
    """Admits email only when the receiving MTA authenticated it (SPF/DKIM/DMARC).

    ``Authentication-Results`` is written by the receiving MTA (Fastmail's
    ``*.messagingengine.com``). We trust only the fields whose authserv-id is the configured
    ``auth_serv_id`` (a domain-boundary match), discarding anything an attacker injected;
    the configured mechanisms must pass, and the DMARC-aligned domain must be in
    ``allowed_domains`` and must match the ``From`` domain, which defeats a spoofed ``From``.
    An optional :class:`SenderAllowlist` narrows it to specific addresses. It fails closed:
    no trusted verdict means no task.
    """

    required: frozenset[str] = frozenset({"dmarc"})
    allowed_domains: frozenset[str] = frozenset()
    senders: SenderAllowlist | None = None
    auth_serv_id: str = DEFAULT_AUTH_SERV_ID

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> EmailAuthGate | None:
        domains = _split_env(env.get("HERALD_AUTH_DOMAINS"))
        if not domains:
            return None
        required = _split_env(env.get("HERALD_AUTH_MECHANISMS")) or frozenset({"dmarc"})
        return cls(
            required=required,
            allowed_domains=domains,
            senders=SenderAllowlist.from_env(env.get("HERALD_ALLOWED_SENDERS")),
            auth_serv_id=env.get("HERALD_AUTH_SERV_ID", DEFAULT_AUTH_SERV_ID),
        )

    def admit(self, *, body: str, headers: dict[str, str], sender: str | None) -> None:
        if self.senders is not None:
            self.senders.admit(body=body, headers=headers, sender=sender)

        raw = _authentication_results(headers)
        if raw is None:
            raise EmailAuthError("no Authentication-Results header")
        trusted = _trusted_authentication_results(raw, self.auth_serv_id)
        if trusted is None:
            raise EmailAuthError(f"Authentication-Results is not from {self.auth_serv_id}")

        verdicts = parse_authentication_results(trusted)
        for mechanism in self.required:
            if verdicts.get(mechanism) != "pass":
                raise EmailAuthError(f"{mechanism} did not pass")
        domain = verdicts.get("dmarc_from") or verdicts.get("dkim_domain")
        if not domain:
            raise EmailAuthError("no authenticated domain in Authentication-Results")
        if self.allowed_domains and domain not in self.allowed_domains:
            raise UnauthorizedSenderError(sender)
        if sender and _domain(sender) != domain:
            raise EmailAuthError("From domain does not match the authenticated domain")


def _split_env(value: str | None) -> frozenset[str]:
    return frozenset(
        part.strip().lower() for part in (value or "").replace(";", ",").split(",") if part.strip()
    )


__all__ = [
    "AuthError",
    "BadSignatureError",
    "EmailAuthError",
    "EmailAuthGate",
    "Gate",
    "InboundAuthorizer",
    "InboundGate",
    "MissingSignatureError",
    "RateLimitedError",
    "RateLimiter",
    "SIGNATURE_HEADER",
    "SenderAllowlist",
    "UnauthorizedSenderError",
    "parse_authentication_results",
    "sign",
]

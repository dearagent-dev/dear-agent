from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from herald.auth import (
    SIGNATURE_HEADER,
    BadSignatureError,
    InboundAuthorizer,
    InboundGate,
    MissingSignatureError,
    RateLimitedError,
    RateLimiter,
    SenderAllowlist,
    UnauthorizedSenderError,
    sign,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)
SENDER = "dev@example.com"
BODY = "repo: https://github.com/owner/repo\n\nfix the build"


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def headers_for(body: str = BODY, secret: str = "s3cret", sender: str = SENDER) -> dict[str, str]:
    return {SIGNATURE_HEADER: sign(body, secret, sender=sender)}


def test_valid_signature_is_accepted() -> None:
    InboundAuthorizer(secret="s3cret").authorize(body=BODY, headers=headers_for(), sender=SENDER)


def test_missing_signature_is_rejected() -> None:
    with pytest.raises(MissingSignatureError):
        InboundAuthorizer(secret="s3cret").authorize(body=BODY, headers={}, sender=SENDER)


def test_wrong_secret_is_rejected() -> None:
    with pytest.raises(BadSignatureError):
        InboundAuthorizer(secret="s3cret").authorize(
            body=BODY, headers=headers_for(secret="other"), sender=SENDER
        )


def test_signature_is_bound_to_the_sender() -> None:
    headers = headers_for(sender=SENDER)

    with pytest.raises(BadSignatureError):
        InboundAuthorizer(secret="s3cret").authorize(
            body=BODY, headers=headers, sender="attacker@example.com"
        )


def test_allowlist_rejects_unknown_sender() -> None:
    authorizer = InboundAuthorizer(secret="s3cret", allowlist=frozenset({SENDER}))

    with pytest.raises(UnauthorizedSenderError):
        authorizer.authorize(
            body=BODY,
            headers=headers_for(sender="stranger@example.com"),
            sender="stranger@example.com",
        )


def test_rate_limiter_allows_up_to_the_limit() -> None:
    clock = FakeClock(BASE)
    limiter = RateLimiter(limit=2, window=timedelta(hours=1), clock=clock)

    limiter.check(SENDER)
    limiter.check(SENDER)


def test_rate_limiter_blocks_beyond_the_limit() -> None:
    clock = FakeClock(BASE)
    limiter = RateLimiter(limit=2, window=timedelta(hours=1), clock=clock)
    limiter.check(SENDER)
    limiter.check(SENDER)

    with pytest.raises(RateLimitedError):
        limiter.check(SENDER)


def test_rate_limiter_window_slides() -> None:
    clock = FakeClock(BASE)
    limiter = RateLimiter(limit=1, window=timedelta(hours=1), clock=clock)
    limiter.check(SENDER)

    clock.advance(timedelta(hours=2))
    limiter.check(SENDER)


def test_rate_limit_is_per_sender() -> None:
    clock = FakeClock(BASE)
    limiter = RateLimiter(limit=1, window=timedelta(hours=1), clock=clock)
    limiter.check(SENDER)

    limiter.check("other@example.com")


def test_sender_allowlist_admits_a_listed_sender() -> None:
    SenderAllowlist(frozenset({SENDER})).admit(body=BODY, headers={}, sender=SENDER)


def test_sender_allowlist_rejects_an_unlisted_sender() -> None:
    allow = SenderAllowlist(frozenset({SENDER}))

    with pytest.raises(UnauthorizedSenderError):
        allow.admit(body=BODY, headers={}, sender="stranger@example.com")


def test_sender_allowlist_is_case_insensitive() -> None:
    SenderAllowlist(frozenset({"dev@example.com"})).admit(
        body=BODY, headers={}, sender="DEV@Example.com"
    )


def test_sender_allowlist_supports_domains() -> None:
    allow = SenderAllowlist(frozenset({"@example.com"}))

    allow.admit(body=BODY, headers={}, sender="anyone@example.com")
    with pytest.raises(UnauthorizedSenderError):
        allow.admit(body=BODY, headers={}, sender="anyone@other.com")


def test_sender_allowlist_from_env_parses_and_ignores_blanks() -> None:
    allow = SenderAllowlist.from_env(" A@x.com , @y.com ;; ")

    assert allow is not None
    assert allow.senders == frozenset({"a@x.com", "@y.com"})


def test_sender_allowlist_from_env_is_none_when_unset_or_empty() -> None:
    assert SenderAllowlist.from_env(None) is None
    assert SenderAllowlist.from_env("  ") is None


def test_empty_sender_allowlist_fails_closed() -> None:
    with pytest.raises(UnauthorizedSenderError):
        SenderAllowlist(frozenset()).admit(body=BODY, headers={}, sender=SENDER)


def test_gate_runs_auth_before_rate_limit() -> None:
    gate = InboundGate(
        authorizer=InboundAuthorizer(secret="s3cret"),
        rate_limiter=RateLimiter(limit=1, window=timedelta(hours=1)),
    )

    with pytest.raises(MissingSignatureError):
        gate.admit(body=BODY, headers={}, sender=SENDER)

    gate.admit(body=BODY, headers=headers_for(), sender=SENDER)
    with pytest.raises(RateLimitedError):
        gate.admit(body=BODY, headers=headers_for(), sender=SENDER)

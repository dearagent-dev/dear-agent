from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dear_agent.auth import (
    SIGNATURE_HEADER,
    BadSignatureError,
    EmailAuthError,
    EmailAuthGate,
    InboundAuthorizer,
    InboundGate,
    MissingSignatureError,
    RateLimitedError,
    RateLimiter,
    SenderAllowlist,
    UnauthorizedSenderError,
    parse_authentication_results,
    sign,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)
SENDER = "dev@example.com"
BODY = "repo: https://github.com/owner/repo\n\nfix the build"

# Trimmed-down shape of what Fastmail adds to a real Gmail message.
GMAIL_AUTH_RESULTS = (
    "phl-mx-07.messagingengine.com;\n"
    "dkim=pass (2048-bit rsa key sha256) header.d=gmail.com header.i=@gmail.com;\n"
    "dmarc=pass policy.published-domain-policy=none header.from=gmail.com"
)


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


def test_parse_authentication_results_extracts_verdicts_and_domains() -> None:
    verdicts = parse_authentication_results(GMAIL_AUTH_RESULTS)

    assert verdicts["dkim"] == "pass"
    assert verdicts["dmarc"] == "pass"
    assert verdicts["dkim_domain"] == "gmail.com"
    assert verdicts["dmarc_from"] == "gmail.com"


def test_parse_authentication_results_keeps_the_first_verdict() -> None:
    raw = "mx; dmarc=fail header.from=example.com\nmx; dmarc=pass header.from=example.com"

    verdicts = parse_authentication_results(raw)

    assert verdicts["dmarc"] == "fail"


def test_email_auth_gate_admits_authenticated_aligned_mail() -> None:
    gate = EmailAuthGate(allowed_domains=frozenset({"gmail.com"}))

    gate.admit(
        body=BODY,
        headers={"Authentication-Results": GMAIL_AUTH_RESULTS},
        sender="ricardo.arguello@gmail.com",
    )


def test_email_auth_gate_rejects_mail_without_authentication_results() -> None:
    gate = EmailAuthGate(allowed_domains=frozenset({"gmail.com"}))

    with pytest.raises(EmailAuthError):
        gate.admit(body=BODY, headers={}, sender="ricardo.arguello@gmail.com")


def test_email_auth_gate_rejects_a_failed_dmarc() -> None:
    raw = GMAIL_AUTH_RESULTS.replace("dmarc=pass", "dmarc=fail")
    gate = EmailAuthGate(allowed_domains=frozenset({"gmail.com"}))

    with pytest.raises(EmailAuthError):
        gate.admit(
            body=BODY,
            headers={"Authentication-Results": raw},
            sender="ricardo.arguello@gmail.com",
        )


def test_email_auth_gate_rejects_a_domain_not_allowed() -> None:
    gate = EmailAuthGate(allowed_domains=frozenset({"example.com"}))

    with pytest.raises(UnauthorizedSenderError):
        gate.admit(
            body=BODY,
            headers={"Authentication-Results": GMAIL_AUTH_RESULTS},
            sender="ricardo.arguello@gmail.com",
        )


def test_email_auth_gate_rejects_a_spoofed_from() -> None:
    # DMARC authenticated gmail.com, but From claims another domain: misaligned.
    gate = EmailAuthGate(allowed_domains=frozenset({"gmail.com"}))

    with pytest.raises(EmailAuthError):
        gate.admit(
            body=BODY,
            headers={"Authentication-Results": GMAIL_AUTH_RESULTS},
            sender="attacker@example.com",
        )


def test_email_auth_gate_rejects_an_untrusted_auth_serv_id() -> None:
    raw = GMAIL_AUTH_RESULTS.replace("messagingengine.com", "evil.example.com")
    gate = EmailAuthGate(allowed_domains=frozenset({"gmail.com"}))

    with pytest.raises(EmailAuthError):
        gate.admit(
            body=BODY,
            headers={"Authentication-Results": raw},
            sender="ricardo.arguello@gmail.com",
        )


def test_email_auth_gate_rejects_a_lookalike_auth_serv_id() -> None:
    raw = "notmessagingengine.com; dmarc=pass header.from=gmail.com"
    gate = EmailAuthGate(allowed_domains=frozenset({"gmail.com"}))

    with pytest.raises(EmailAuthError):
        gate.admit(
            body=BODY,
            headers={"Authentication-Results": raw},
            sender="ricardo.arguello@gmail.com",
        )


def test_email_auth_gate_ignores_an_injected_pass_header() -> None:
    trusted_fail = GMAIL_AUTH_RESULTS.replace("dmarc=pass", "dmarc=fail")
    injected = "attacker.example.com; dmarc=pass header.from=gmail.com"
    gate = EmailAuthGate(allowed_domains=frozenset({"gmail.com"}))

    with pytest.raises(EmailAuthError):
        gate.admit(
            body=BODY,
            headers={"Authentication-Results": f"{trusted_fail}\n{injected}"},
            sender="ricardo.arguello@gmail.com",
        )


def test_email_auth_gate_first_trusted_verdict_wins() -> None:
    # Even a field claiming the trusted authserv-id cannot upgrade an earlier fail.
    raw = (
        "phl-mx-07.messagingengine.com; dmarc=fail header.from=gmail.com\n"
        "phl-mx-07.messagingengine.com; dmarc=pass header.from=gmail.com"
    )
    gate = EmailAuthGate(allowed_domains=frozenset({"gmail.com"}))

    with pytest.raises(EmailAuthError):
        gate.admit(
            body=BODY,
            headers={"Authentication-Results": raw},
            sender="ricardo.arguello@gmail.com",
        )


def test_email_auth_gate_accepts_per_mechanism_trusted_headers() -> None:
    raw = (
        "phl-mx-07.messagingengine.com; dkim=pass header.d=gmail.com\n"
        "phl-mx-07.messagingengine.com; dmarc=pass header.from=gmail.com"
    )
    gate = EmailAuthGate(allowed_domains=frozenset({"gmail.com"}))

    gate.admit(
        body=BODY,
        headers={"Authentication-Results": raw},
        sender="ricardo.arguello@gmail.com",
    )


def test_email_auth_gate_applies_the_sender_allowlist() -> None:
    gate = EmailAuthGate(
        allowed_domains=frozenset({"gmail.com"}),
        senders=SenderAllowlist(frozenset({"other@gmail.com"})),
    )

    with pytest.raises(UnauthorizedSenderError):
        gate.admit(
            body=BODY,
            headers={"Authentication-Results": GMAIL_AUTH_RESULTS},
            sender="ricardo.arguello@gmail.com",
        )


def test_email_auth_gate_from_env() -> None:
    gate = EmailAuthGate.from_env(
        {
            "DEAR_AGENT_AUTH_DOMAINS": "gmail.com",
            "DEAR_AGENT_ALLOWED_SENDERS": "ricardo.arguello@gmail.com",
        }
    )

    assert gate is not None
    assert gate.allowed_domains == frozenset({"gmail.com"})
    assert gate.senders is not None


def test_email_auth_gate_from_env_is_none_without_domains() -> None:
    assert EmailAuthGate.from_env({}) is None
    assert EmailAuthGate.from_env({"DEAR_AGENT_ALLOWED_SENDERS": "x@y.com"}) is None


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

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from dear_agent.auth import InboundAuthorizer, InboundGate, RateLimiter
from dear_agent.control_plane import ControlPlane
from dear_agent.inbound import InboundError, InboundWebhook, parse_message
from dear_agent.queue.memory import MemoryQueue
from dear_agent.queue.models import TaskState
from dear_agent.transports.memory import MemoryTransport


def _gate(secret: str = "s3cret") -> InboundGate:
    return InboundGate(
        authorizer=InboundAuthorizer(secret=secret),
        rate_limiter=RateLimiter(limit=100, window=timedelta(minutes=1)),
    )


def _webhook(secret: str = "s3cret") -> tuple[InboundWebhook, MemoryQueue]:
    queue = MemoryQueue()
    plane = ControlPlane(transport=MemoryTransport(), queue=queue)
    return InboundWebhook(control_plane=plane, gate=_gate(secret)), queue


def test_parse_message_reads_the_minimal_payload() -> None:
    message = parse_message(b'{"id": "<m1@x>", "body": "repo: o/r\\nadd healthz"}')

    assert message.transport_id == "<m1@x>"
    assert message.body == "repo: o/r\nadd healthz"


def test_parse_rejects_a_payload_without_an_id() -> None:
    with pytest.raises(InboundError):
        parse_message(b'{"body": "hi"}')


def test_parse_rejects_non_json() -> None:
    with pytest.raises(InboundError):
        parse_message(b"not json")


def _signed(secret: str, message: dict) -> tuple[bytes, dict[str, str]]:
    from dear_agent.auth import SIGNATURE_HEADER, sign

    body = json.dumps(message)
    signature = sign(body, secret, sender=message.get("sender"))
    return body.encode(), {SIGNATURE_HEADER: signature}


def test_webhook_ingests_a_signed_message() -> None:
    webhook, queue = _webhook()
    message = {
        "id": "<m1@x>",
        "sender": "dev@example.com",
        "subject": "add healthz",
        "body": "repo: https://github.com/o/r\nadd a /healthz endpoint",
    }

    status, payload = webhook(*_signed("s3cret", message))

    assert status == 202
    assert payload["accepted"] == ["<m1@x>"]
    assert len(queue.list(TaskState.QUEUED)) == 1


def test_webhook_rejects_a_bad_signature() -> None:
    webhook, _ = _webhook()
    message = {"id": "<m1@x>", "body": "repo: o/r\nhi"}
    body, headers = _signed("wrong", message)

    status, payload = webhook(body, headers)

    assert status == 401
    assert "error" in payload


def test_webhook_rejects_a_malformed_payload_before_auth() -> None:
    webhook, _ = _webhook()

    status, _ = webhook(b"not json", {})

    assert status == 400

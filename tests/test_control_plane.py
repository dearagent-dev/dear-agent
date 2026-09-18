from __future__ import annotations

from datetime import UTC, datetime, timedelta

from herald.auth import InboundAuthorizer, InboundGate, RateLimiter, sign
from herald.control_plane import REJECT_BODIES, ControlPlane
from herald.normalizer import RejectReason
from herald.notify.notifier import Notifier
from herald.queue.memory import MemoryQueue
from herald.queue.models import TaskState
from herald.transports.base import Attachment, RawMessage
from herald.transports.memory import MemoryTransport

BASE = datetime(2026, 1, 1, tzinfo=UTC)
SENDER = "dev@example.com"
SECRET = "s3cret"
BODY = "repo: https://github.com/owner/repo\n\nfix the build"


def make_message(body: str = BODY, **overrides: object) -> RawMessage:
    values: dict[str, object] = {
        "transport_id": "<m1@x>",
        "thread_id": "t1",
        "sender": SENDER,
        "subject": "[herald] owner/repo: fix the build",
        "body": body,
        "headers": {"X-Herald-Signature": sign(body, SECRET, sender=SENDER)},
        "received_at": BASE,
    }
    values.update(overrides)
    return RawMessage(**values)  # type: ignore[arg-type]


def make_gate() -> InboundGate:
    return InboundGate(
        authorizer=InboundAuthorizer(secret=SECRET),
        rate_limiter=RateLimiter(limit=100, window=timedelta(hours=1)),
    )


def test_accepted_message_is_enqueued() -> None:
    queue = MemoryQueue()
    transport = MemoryTransport()
    plane = ControlPlane(transport=transport, queue=queue, gate=make_gate())

    report = plane.ingest([make_message()])

    assert report.accepted == ["<m1@x>"]
    assert queue.list(TaskState.QUEUED)[0].id == "<m1@x>"


def test_redelivery_is_idempotent() -> None:
    queue = MemoryQueue()
    plane = ControlPlane(transport=MemoryTransport(), queue=queue, gate=make_gate())

    plane.ingest([make_message()])
    report = plane.ingest([make_message()])

    assert report.accepted == []
    assert len(queue.list(TaskState.QUEUED)) == 1


def test_unauthorized_message_is_denied_and_dropped() -> None:
    queue = MemoryQueue()
    transport = MemoryTransport()
    plane = ControlPlane(
        transport=transport, queue=queue, notifier=Notifier(transport), gate=make_gate()
    )
    unsigned = make_message(headers={})

    report = plane.ingest([unsigned], recipient="ops@example.com")

    assert report.denied == ["<m1@x>"]
    assert queue.list(TaskState.QUEUED) == []
    assert transport.outbox == []


def test_message_with_attachments_is_rejected_with_a_reply() -> None:
    queue = MemoryQueue()
    transport = MemoryTransport()
    plane = ControlPlane(
        transport=transport, queue=queue, notifier=Notifier(transport), gate=make_gate()
    )
    message = make_message(
        attachments=[Attachment(name="patch.diff", content_type="text/x-patch")],
        headers={"X-Herald-Signature": sign(BODY, SECRET, sender=SENDER)},
    )

    report = plane.ingest([message], recipient="ops@example.com")

    assert report.rejected == ["<m1@x>"]
    assert transport.outbox[0].body == REJECT_BODIES[RejectReason.ATTACHMENTS]
    assert queue.list(TaskState.QUEUED) == []


def test_message_without_repo_is_rejected() -> None:
    queue = MemoryQueue()
    transport = MemoryTransport()
    plane = ControlPlane(
        transport=transport, queue=queue, notifier=Notifier(transport), gate=make_gate()
    )
    body = "just do something"
    message = make_message(
        body=body, headers={"X-Herald-Signature": sign(body, SECRET, sender=SENDER)}
    )

    report = plane.ingest([message], recipient="ops@example.com")

    assert report.rejected == ["<m1@x>"]
    assert "repository" in transport.outbox[0].body


def test_no_gate_means_no_auth() -> None:
    queue = MemoryQueue()
    plane = ControlPlane(transport=MemoryTransport(), queue=queue)

    report = plane.ingest([make_message(headers={})])

    assert report.accepted == ["<m1@x>"]

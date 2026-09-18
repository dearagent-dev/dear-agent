from __future__ import annotations

from datetime import UTC, datetime, timedelta

from herald.transports.base import Attachment, OutboundMessage, RawMessage
from herald.transports.memory import MemoryTransport
from herald.transports.port import Transport

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_message(message_id: str = "<m1@x>", **overrides: object) -> RawMessage:
    values: dict[str, object] = {
        "transport_id": message_id,
        "thread_id": "t1",
        "sender": "dev@example.com",
        "subject": "[herald] owner/repo: add health endpoint",
        "body": "please add a /healthz endpoint",
        "received_at": BASE,
    }
    values.update(overrides)
    return RawMessage(**values)  # type: ignore[arg-type]


def test_memory_transport_satisfies_the_port() -> None:
    assert isinstance(MemoryTransport(), Transport)


def test_receive_then_poll_returns_the_message() -> None:
    transport = MemoryTransport()
    message = make_message()

    transport.receive(message)

    assert transport.poll() == [message]


def test_poll_drains_pending() -> None:
    transport = MemoryTransport()
    transport.receive(make_message())

    assert len(transport.poll()) == 1
    assert transport.poll() == []


def test_redelivery_with_the_same_transport_id_is_not_duplicated() -> None:
    transport = MemoryTransport()
    transport.receive(make_message("<m1@x>", subject="first"))
    transport.receive(make_message("<m1@x>", subject="second"))

    polled = transport.poll()

    assert len(polled) == 1
    assert polled[0].subject == "second"


def test_poll_orders_oldest_first() -> None:
    transport = MemoryTransport()
    transport.receive(make_message("<m2@x>", received_at=BASE + timedelta(minutes=1)))
    transport.receive(make_message("<m1@x>", received_at=BASE))

    assert [message.transport_id for message in transport.poll()] == ["<m1@x>", "<m2@x>"]


def test_send_records_the_outbound_message() -> None:
    transport = MemoryTransport()
    message = OutboundMessage(
        thread_id="t1",
        subject="re: task",
        body="queued",
        headers={"X-Herald-Task": "e1"},
    )

    transport.send(message)

    assert transport.outbox == [message]


def test_outbound_message_has_no_attachment_field() -> None:
    assert "attachments" not in OutboundMessage.__dataclass_fields__


def test_raw_message_reports_attachments() -> None:
    message = make_message(
        attachments=[Attachment(name="patch.diff", content_type="text/x-patch", size=42)]
    )

    assert message.has_attachments is True
    assert message.attachments[0].name == "patch.diff"


def test_raw_message_without_attachments() -> None:
    assert make_message().has_attachments is False

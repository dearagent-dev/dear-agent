from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from herald.jmap.client import EmailRecord, _extract_text_body
from herald.transports.base import OutboundMessage
from herald.transports.jmap import JmapTransport
from herald.transports.port import Transport

BASE = datetime(2026, 1, 1, tzinfo=UTC)


class FakeMailClient:
    def __init__(self) -> None:
        self.records: dict[str, EmailRecord] = {}
        self.sent: list[dict[str, object]] = []
        self._mailboxes = {
            "Herald": "mbx:herald",
            "Herald-Done": "mbx:done",
            "Sent": "mbx:sent",
        }

    def add(self, record: EmailRecord) -> EmailRecord:
        self.records[record.id] = record
        return record

    def query_ids(self, *, filter: dict, limit: int) -> list[str]:
        mailbox = filter.get("inMailbox")
        not_keyword = filter.get("notKeyword")
        matched = []
        for record in self.records.values():
            if mailbox is not None and mailbox not in record.mailbox_ids:
                continue
            if not_keyword is not None and not_keyword in record.keywords:
                continue
            matched.append(record)
        matched.sort(key=lambda record: (record.received_at, record.id))
        return [record.id for record in matched[:limit]]

    def get(self, ids: list[str], *, fetch_body: bool = False) -> tuple[str, list[EmailRecord]]:
        return "S1", [self.records[email_id] for email_id in ids if email_id in self.records]

    def get_or_create_mailbox(self, name: str) -> str:
        if name not in self._mailboxes:
            self._mailboxes[name] = f"mbx:{name.lower()}"
        return self._mailboxes[name]

    def update(self, email_id: str, patch: dict, *, if_in_state: str) -> str:
        record = self.records[email_id]
        for key, value in patch.items():
            kind, _, name = key.partition("/")
            target = record.mailbox_ids if kind == "mailboxIds" else record.keywords
            if value:
                target.add(name)
            else:
                target.discard(name)
        return "S2"

    def submit(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        in_reply_to_message_id: str | None = None,
    ) -> str:
        self.sent.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "in_reply_to_message_id": in_reply_to_message_id,
            }
        )
        return "submitted-1"


def record(record_id: str = "e1", **overrides: object) -> EmailRecord:
    values: dict[str, object] = {
        "id": record_id,
        "message_id": f"<{record_id}@x>",
        "thread_id": "t1",
        "sender": "dev@example.com",
        "subject": "a task",
        "received_at": BASE,
        "keywords": set(),
        "mailbox_ids": {"mbx:herald"},
        "body": "repo: https://github.com/owner/repo\n\nfix the build",
    }
    values.update(overrides)
    return EmailRecord(**values)  # type: ignore[arg-type]


def test_jmap_transport_satisfies_the_port() -> None:
    assert isinstance(JmapTransport(FakeMailClient()), Transport)


def test_poll_maps_records_to_raw_messages() -> None:
    client = FakeMailClient()
    client.add(record())

    polled = JmapTransport(client).poll()

    assert len(polled) == 1
    message = polled[0]
    assert message.transport_id == "<e1@x>"
    assert message.thread_id == "t1"
    assert message.sender == "dev@example.com"
    assert message.subject == "a task"
    assert "fix the build" in message.body
    assert message.attachments == []


def test_poll_orders_oldest_first() -> None:
    client = FakeMailClient()
    client.add(record("e2", received_at=BASE + timedelta(minutes=1)))
    client.add(record("e1", received_at=BASE))

    assert [message.transport_id for message in JmapTransport(client).poll()] == [
        "<e1@x>",
        "<e2@x>",
    ]


def test_poll_skips_seen_messages() -> None:
    client = FakeMailClient()
    client.add(record("e1", keywords={"$herald-seen"}))

    assert JmapTransport(client).poll() == []


def test_poll_reports_attachments_so_the_normalizer_can_reject() -> None:
    client = FakeMailClient()
    client.add(record(has_attachment=True))

    polled = JmapTransport(client).poll()

    assert polled[0].has_attachments is True


def test_to_headers_joins_duplicate_names() -> None:
    from herald.jmap.client import _to_headers

    headers = _to_headers(
        [
            {"name": "Authentication-Results", "value": "a"},
            {"name": "Authentication-Results", "value": "b"},
            {"name": "From", "value": "x@y"},
        ]
    )

    assert headers["Authentication-Results"] == "a\nb"
    assert headers["From"] == "x@y"


def test_poll_passes_headers_through_for_the_auth_gate() -> None:
    client = FakeMailClient()
    client.add(
        record(
            "e1",
            headers={"Authentication-Results": "mx; dmarc=pass header.from=example.com"},
        )
    )

    polled = JmapTransport(client).poll()

    assert "dmarc=pass" in polled[0].headers["Authentication-Results"]


def test_ack_files_processed_messages_so_they_are_not_polled_again() -> None:
    client = FakeMailClient()
    client.add(record("e1"))
    transport = JmapTransport(client)

    messages = transport.poll()
    transport.ack(messages)

    assert client.records["e1"].mailbox_ids == {"mbx:done"}
    assert transport.poll() == []


def test_send_submits_threaded_mail() -> None:
    client = FakeMailClient()
    transport = JmapTransport(client)
    message = OutboundMessage(
        thread_id="t1",
        subject="re: a task",
        body="queued",
        headers={"to": "dev@example.com", "in-reply-to": "<e1@x>"},
    )

    transport.send(message)

    assert client.sent == [
        {
            "to": "dev@example.com",
            "subject": "re: a task",
            "body": "queued",
            "in_reply_to_message_id": "<e1@x>",
        }
    ]


def test_send_requires_a_thread_and_recipient() -> None:
    transport = JmapTransport(FakeMailClient())

    with pytest.raises(ValueError):
        transport.send(OutboundMessage(thread_id=None, subject="s", body="b", headers={"to": "x"}))

    with pytest.raises(ValueError):
        transport.send(OutboundMessage(thread_id="t1", subject="s", body="b"))


def test_receive_is_not_supported_for_a_pull_transport() -> None:
    with pytest.raises(NotImplementedError):
        JmapTransport(FakeMailClient()).receive(record())  # type: ignore[arg-type]


def test_extract_text_body_joins_text_parts() -> None:
    item = {
        "textBody": [{"partId": "p1", "type": "text/plain"}],
        "bodyValues": {"p1": {"value": "hello"}},
    }

    assert _extract_text_body(item) == "hello"


def test_extract_text_body_is_empty_without_body_values() -> None:
    assert _extract_text_body({}) == ""

from __future__ import annotations

from typing import Any, Protocol

from dear_agent.jmap.client import EmailRecord
from dear_agent.transports.base import Attachment, OutboundMessage, RawMessage


class _MailBoxClient(Protocol):
    """Slice of :class:`~dear_agent.jmap.client.JmapClient` the transport depends on."""

    def query_ids(self, *, filter: dict[str, Any], limit: int) -> list[str]: ...
    def get(self, ids: list[str], *, fetch_body: bool = False) -> tuple[str, list[EmailRecord]]: ...
    def get_or_create_mailbox(self, name: str) -> str: ...
    def update(self, email_id: str, patch: dict[str, Any], *, if_in_state: str) -> str: ...
    def submit(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        in_reply_to_message_id: str | None = None,
    ) -> str: ...


class JmapTransport:
    """Transport over Fastmail JMAP.

    Inbound pulls messages from the task mailbox as :class:`RawMessage`; outbound submits
    status/approval mail threaded to the original conversation. It contains no business
    logic: mapping to tasks is the Normalizer's job.
    """

    def __init__(
        self,
        client: _MailBoxClient,
        *,
        mailbox_name: str = "Dear Agent",
        processed_mailbox_name: str = "Dear-Agent-Done",
    ) -> None:
        self._client = client
        self._mailbox_name = mailbox_name
        self._processed_mailbox_name = processed_mailbox_name
        self._pending_ids: dict[str, str] = {}

    def poll(self, *, limit: int = 50) -> list[RawMessage]:
        mailbox_id = self._client.get_or_create_mailbox(self._mailbox_name)
        ids = self._client.query_ids(
            filter={"inMailbox": mailbox_id, "notKeyword": "$dear-agent-seen"},
            limit=limit,
        )
        if not ids:
            return []
        _, records = self._client.get(ids, fetch_body=True)
        records.sort(key=lambda record: (record.received_at, record.id))
        # Remember the JMAP email id behind each transport id so :meth:`ack` can file them.
        self._pending_ids = {(record.message_id or record.id): record.id for record in records}
        return [self._to_raw(record) for record in records]

    def ack(self, messages: list[RawMessage]) -> None:
        """File handled messages into the processed mailbox so they are not redelivered.

        Best effort: a failure here leaves the message for the queue's dedupe to absorb.
        """
        if not messages or not self._pending_ids:
            return
        source = self._client.get_or_create_mailbox(self._mailbox_name)
        target = self._client.get_or_create_mailbox(self._processed_mailbox_name)
        for message in messages:
            email_id = self._pending_ids.pop(message.transport_id, None)
            if email_id is None:
                continue
            try:
                state, records = self._client.get([email_id])
                if not records:
                    continue
                self._client.update(
                    email_id,
                    {f"mailboxIds/{target}": True, f"mailboxIds/{source}": None},
                    if_in_state=state,
                )
            except Exception:  # noqa: BLE001 - acknowledging is best effort
                continue

    def receive(self, message: RawMessage) -> None:
        raise NotImplementedError("JMAP is a pull transport; use poll()")

    def send(self, message: OutboundMessage) -> None:
        if message.thread_id is None:
            raise ValueError("outbound message needs a thread_id to reply into")
        recipient = message.headers.get("to")
        if not recipient:
            raise ValueError("outbound message needs a 'to' header")
        self._client.submit(
            to=recipient,
            subject=message.subject,
            body=message.body,
            in_reply_to_message_id=message.headers.get("in-reply-to"),
        )

    @staticmethod
    def _to_raw(record: EmailRecord) -> RawMessage:
        attachments = (
            [Attachment(name="attachment", content_type="application/octet-stream")]
            if record.has_attachment
            else []
        )
        return RawMessage(
            transport_id=record.message_id or record.id,
            thread_id=record.thread_id,
            sender=record.sender,
            subject=record.subject,
            body=record.body,
            attachments=attachments,
            headers=dict(record.headers),
            received_at=record.received_at,
        )

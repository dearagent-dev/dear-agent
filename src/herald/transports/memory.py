from __future__ import annotations

from herald.transports.base import OutboundMessage, RawMessage


class MemoryTransport:
    """In-memory :class:`~herald.transports.port.Transport` for tests.

    Inbound messages can be pushed with :meth:`receive` or queued for :meth:`poll`. A
    redelivery with the same ``transport_id`` replaces the pending copy instead of
    queueing a duplicate, mirroring a store-and-forward mailbox. Sent messages are kept
    in :attr:`outbox` for assertions.
    """

    def __init__(self) -> None:
        self._pending: dict[str, RawMessage] = {}
        self.outbox: list[OutboundMessage] = []

    def receive(self, message: RawMessage) -> None:
        self._pending[message.transport_id] = message

    def poll(self) -> list[RawMessage]:
        messages = sorted(self._pending.values(), key=lambda msg: msg.received_at)
        self._pending.clear()
        return messages

    def send(self, message: OutboundMessage) -> None:
        self.outbox.append(message)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

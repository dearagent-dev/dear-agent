from __future__ import annotations

from typing import Protocol, runtime_checkable

from herald.transports.base import OutboundMessage, RawMessage


@runtime_checkable
class Transport(Protocol):
    """Async message transport.

    Inbound may be pull (a poller) or push (a webhook handler); both yield the same
    :class:`~herald.transports.base.RawMessage`. Outbound sends status and approval
    messages threaded to the original conversation. Transports contain no business logic
    and never carry source code.
    """

    def poll(self) -> list[RawMessage]:
        """Return messages available now (pull transport), oldest first."""
        ...

    def receive(self, message: RawMessage) -> None:
        """Accept a pushed message (webhook/push transport)."""
        ...

    def send(self, message: OutboundMessage) -> None:
        """Send ``message`` in-thread; must not carry source or attachments."""
        ...

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


class TransportError(Exception):
    """Base class for transport errors."""


@dataclass(slots=True, frozen=True)
class Attachment:
    """An inbound attachment.

    Attachments are reported so the Normalizer can reject code travelling over the
    transport; Herald never stores or forwards them.
    """

    name: str
    content_type: str
    size: int = 0


@dataclass(slots=True)
class RawMessage:
    """A message as received from a transport, before normalization.

    ``transport_id`` is the transport's stable identifier (``Message-ID``, JMAP
    ``emailId``, webhook event id) and is the dedupe key. ``body`` is untrusted text.
    """

    transport_id: str
    thread_id: str | None = None
    sender: str | None = None
    subject: str | None = None
    body: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)


@dataclass(slots=True, frozen=True)
class OutboundMessage:
    """A status or approval message to send.

    Deliberately has no attachment field: **no source code travels over the transport**,
    only metadata and links. ``headers`` carries threading/approval tokens.
    """

    thread_id: str | None
    subject: str
    body: str
    headers: dict[str, str] = field(default_factory=dict)

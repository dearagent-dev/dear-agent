from __future__ import annotations

from dataclasses import dataclass, field

from herald.auth import AuthError, InboundGate
from herald.normalizer import NormalizedTask, Rejected, RejectReason, normalize
from herald.notify.notifier import Notifier
from herald.queue.port import Queue
from herald.transports.base import OutboundMessage, RawMessage
from herald.transports.port import Transport

REJECT_BODIES: dict[RejectReason, str] = {
    RejectReason.ATTACHMENTS: (
        "Herald does not accept source code, patches or attachments. "
        "Code travels over Git only; resend your task as plain text with a repo and "
        "instructions."
    ),
    RejectReason.NO_REPO: (
        "Herald could not find a repository. Add a 'repo: <url>' line or send to an "
        "owner-repo@ routing address."
    ),
    RejectReason.EMPTY: (
        "Herald could not find instructions. Describe the task in the message body."
    ),
}


@dataclass(slots=True)
class IngestReport:
    """What happened to each polled message."""

    accepted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)


class ControlPlane:
    """The inbound loop: authorize, normalize, enqueue, and reply.

    For each message it applies the auth gate first, then the Normalizer, then the queue.
    A rejected task gets a threaded explanation; an unauthorized one is dropped (never
    normalized and never answered, to avoid backscatter).
    """

    def __init__(
        self,
        *,
        transport: Transport,
        queue: Queue,
        notifier: Notifier | None = None,
        gate: InboundGate | None = None,
    ) -> None:
        self._transport = transport
        self._queue = queue
        self._notifier = notifier
        self._gate = gate

    def ingest(self, messages: list[RawMessage], *, recipient: str | None = None) -> IngestReport:
        report = IngestReport()
        for message in messages:
            try:
                self._authorize(message)
            except AuthError:
                report.denied.append(message.transport_id)
                continue

            result = normalize(message, recipient=recipient)
            if isinstance(result, Rejected):
                report.rejected.append(message.transport_id)
                self._reply_rejection(message, result, recipient)
                continue

            assert isinstance(result, NormalizedTask)
            if self._queue.enqueue(result.task) is None:
                # Idempotent: a redelivery is a no-op, not an error.
                continue
            report.accepted.append(result.task.id)
        return report

    def _authorize(self, message: RawMessage) -> None:
        if self._gate is None:
            return
        self._gate.admit(body=message.body, headers=message.headers, sender=message.sender)

    def _reply_rejection(
        self, message: RawMessage, rejected: Rejected, recipient: str | None
    ) -> None:
        if self._notifier is None or recipient is None:
            return
        subject = f"[herald] rejected: {message.subject or message.transport_id}"
        self._transport.send(
            OutboundMessage(
                thread_id=message.thread_id,
                subject=subject,
                body=REJECT_BODIES[rejected.reason],
                headers={"to": recipient, "in-reply-to": message.transport_id},
            )
        )


__all__ = ["ControlPlane", "IngestReport", "REJECT_BODIES"]

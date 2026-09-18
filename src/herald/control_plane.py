from __future__ import annotations

from dataclasses import dataclass, field

from herald.approvals import ApprovalError
from herald.approvals_service import ApprovalReply, ApprovalService, parse_reply
from herald.auth import AuthError, InboundGate
from herald.normalizer import NormalizedTask, Rejected, RejectReason, normalize
from herald.notify.notifier import Notifier
from herald.queue.port import Queue
from herald.security import InjectionScanner
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
    decided: list[str] = field(default_factory=list)
    suspicious: list[str] = field(default_factory=list)


class ControlPlane:
    """The inbound loop: authorize, decide approvals, normalize, enqueue, and reply.

    For each message it applies the auth gate first. A message that carries an approval
    decision (`approve|reject <token>`) is applied to its task and not normalized. Anything
    else goes through the Normalizer and the queue. A rejected task gets a threaded
    explanation; an unauthorized one is dropped (never normalized and never answered, to
    avoid backscatter).

    When a scanner is configured, messages that look like prompt injection are still
    ingested but recorded as suspicious, so the run can be gated or audited.
    """

    def __init__(
        self,
        *,
        transport: Transport,
        queue: Queue,
        notifier: Notifier | None = None,
        gate: InboundGate | None = None,
        approvals: ApprovalService | None = None,
        scanner: InjectionScanner | None = None,
    ) -> None:
        self._transport = transport
        self._queue = queue
        self._notifier = notifier
        self._gate = gate
        self._approvals = approvals
        self._scanner = scanner

    def ingest(self, messages: list[RawMessage], *, recipient: str | None = None) -> IngestReport:
        report = IngestReport()
        for message in messages:
            try:
                self._authorize(message)
            except AuthError:
                report.denied.append(message.transport_id)
                continue

            decision = parse_reply(message.body)
            if decision is not None and self._approvals is not None:
                if self._apply_decision(message, decision, recipient):
                    report.decided.append(message.transport_id)
                continue

            result = normalize(message, recipient=recipient)
            if isinstance(result, Rejected):
                report.rejected.append(message.transport_id)
                self._reply_rejection(message, result, recipient)
                continue

            assert isinstance(result, NormalizedTask)
            if self._scanner is not None and self._scanner.scan(message.body).suspicious:
                report.suspicious.append(message.transport_id)
            if self._queue.enqueue(result.task) is None:
                # Idempotent: a redelivery is a no-op, not an error.
                continue
            report.accepted.append(result.task.id)
        return report

    def _apply_decision(
        self, message: RawMessage, decision: ApprovalReply, recipient: str | None
    ) -> bool:
        assert self._approvals is not None
        try:
            self._approvals.apply(decision)
        except ApprovalError as exc:
            self._reply_rejection_message(message, str(exc), recipient)
            return False
        return True

    def _authorize(self, message: RawMessage) -> None:
        if self._gate is None:
            return
        self._gate.admit(body=message.body, headers=message.headers, sender=message.sender)

    def _reply_rejection(
        self, message: RawMessage, rejected: Rejected, recipient: str | None
    ) -> None:
        self._reply_rejection_message(message, REJECT_BODIES[rejected.reason], recipient)

    def _reply_rejection_message(
        self, message: RawMessage, body: str, recipient: str | None
    ) -> None:
        if self._notifier is None or recipient is None:
            return
        subject = f"[herald] could not process: {message.subject or message.transport_id}"
        self._transport.send(
            OutboundMessage(
                thread_id=message.thread_id,
                subject=subject,
                body=body,
                headers={"to": recipient, "in-reply-to": message.transport_id},
            )
        )


__all__ = ["ControlPlane", "IngestReport", "REJECT_BODIES"]

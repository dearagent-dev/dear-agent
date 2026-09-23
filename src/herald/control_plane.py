from __future__ import annotations

from dataclasses import dataclass, field

from herald.approvals import ApprovalError
from herald.approvals_service import ApprovalReply, ApprovalService, parse_reply
from herald.auth import AuthError, Gate
from herald.decision.security import SecurityDecider
from herald.events import EventLog
from herald.normalizer import NormalizedTask, Rejected, RejectReason, normalize
from herald.notify.notifier import Notifier
from herald.policy import PolicyStore
from herald.queue.models import Task, TaskState
from herald.queue.port import Queue, QueueError
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
    RejectReason.NOT_ALLOWED: (
        "Herald is not allowed to work on that repository. Ask an operator to add it to "
        "the project policy."
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
    gated: list[str] = field(default_factory=list)


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
        gate: Gate | None = None,
        approvals: ApprovalService | None = None,
        scanner: InjectionScanner | None = None,
        security: SecurityDecider | None = None,
        events: EventLog | None = None,
        policies: PolicyStore | None = None,
    ) -> None:
        self._transport = transport
        self._queue = queue
        self._notifier = notifier
        self._gate = gate
        self._approvals = approvals
        self._scanner = scanner
        self._security = security
        self._events = events
        self._policies = policies

    def _emit(self, task_id: str, kind: str, **data: object) -> None:
        if self._events is None:
            return
        try:
            self._events.record(task_id, kind, **data)
        except Exception:  # noqa: BLE001 - events are best effort
            return

    def ingest(self, messages: list[RawMessage], *, recipient: str | None = None) -> IngestReport:
        report = IngestReport()
        for message in messages:
            try:
                self._authorize(message)
            except AuthError:
                report.denied.append(message.transport_id)
                self._emit(message.transport_id, "task.denied")
                continue

            decision = parse_reply(message.body)
            if decision is not None and self._approvals is not None:
                if self._apply_decision(message, decision, recipient):
                    report.decided.append(message.transport_id)
                continue

            result = normalize(message, recipient=recipient)
            if isinstance(result, Rejected):
                report.rejected.append(message.transport_id)
                self._emit(message.transport_id, "task.rejected", reason=result.reason.value)
                self._reply_rejection(message, result, recipient)
                continue

            assert isinstance(result, NormalizedTask)
            if not self._repo_allowed(result.spec.repo_url):
                rejected = Rejected(
                    transport_id=message.transport_id,
                    reason=RejectReason.NOT_ALLOWED,
                    detail=result.spec.repo_url,
                )
                report.rejected.append(message.transport_id)
                self._emit(
                    message.transport_id,
                    "task.rejected",
                    reason=RejectReason.NOT_ALLOWED.value,
                    repo=result.spec.repo_url,
                )
                self._reply_rejection(message, rejected, recipient)
                continue

            suspicious = self._flag_suspicious(message, report)
            stored = self._queue.enqueue(result.task)
            if stored is None:
                # Idempotent: a redelivery is a no-op, not an error.
                continue
            report.accepted.append(result.task.id)
            self._emit(result.task.id, "task.accepted", repo=result.spec.repo_url)
            if suspicious and self._approvals is not None:
                self._gate_for_approval(stored, recipient)
                report.gated.append(result.task.id)
        self._ack(messages)
        return report

    def _repo_allowed(self, repo: str) -> bool:
        """A repo is allowed when an enabled project policy matches it.

        With no policies configured the deployment is open; once at least one policy exists
        it is enforced, so an unknown or disabled repo is refused — one cannot ask Herald to
        work on a repository the operator did not allow.
        """
        if self._policies is None:
            return True
        policies = self._policies.list()
        if not policies:
            return True
        policy = self._policies.for_repo(repo)
        return policy is not None and policy.enabled

    def _flag_suspicious(self, message: RawMessage, report: IngestReport) -> bool:
        suspicious = False
        if self._scanner is not None and self._scanner.scan(message.body).suspicious:
            report.suspicious.append(message.transport_id)
            suspicious = True
        if self._security is not None:
            verdict = self._security.assess(message.body)
            if verdict.suspicious and message.transport_id not in report.suspicious:
                report.suspicious.append(message.transport_id)
                suspicious = True
        return suspicious

    def _gate_for_approval(self, task: Task, recipient: str | None) -> None:
        """Park a suspicious task in ``action`` until a human approves it."""
        self._queue.transition(task, TaskState.ACTION)
        self._emit(task.id, "approval.requested", reason="suspicious")
        if self._notifier is not None and recipient:
            self._notifier.request_approval(
                task,
                recipient=recipient,
                action="run",
                summary="message flagged as suspicious; approve to run it",
            )
        else:
            assert self._approvals is not None
            self._approvals.request(task.id, "run")

    def _ack(self, messages: list[RawMessage]) -> None:
        """Tell the transport these messages were handled, so they are not redelivered.

        Acknowledging is best effort: a failure here must never break ingestion, and the
        queue's dedupe already makes a redelivery safe.
        """
        ack = getattr(self._transport, "ack", None)
        if ack is None:
            return
        try:
            ack(messages)
        except Exception:  # noqa: BLE001 - acknowledging is best effort
            return

    def _apply_decision(
        self, message: RawMessage, decision: ApprovalReply, recipient: str | None
    ) -> bool:
        assert self._approvals is not None
        try:
            self._approvals.apply(decision)
        except (ApprovalError, QueueError) as exc:
            # An unknown/used/expired token, or a task that already moved on: explain and
            # keep the loop alive rather than failing the whole ingest.
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

from __future__ import annotations

from dataclasses import dataclass, field

from dear_agent.approvals import ApprovalError
from dear_agent.approvals_service import ApprovalReply, ApprovalService, parse_reply
from dear_agent.auth import AuthError, Gate
from dear_agent.decision.router import HumanGate
from dear_agent.decision.security import SecurityDecider
from dear_agent.events import EventLog
from dear_agent.normalizer import NormalizedTask, Rejected, RejectReason, normalize
from dear_agent.notify.notifier import Notifier
from dear_agent.policy import PolicyStore
from dear_agent.queue.models import Task, TaskState
from dear_agent.queue.port import Queue, QueueError
from dear_agent.security import InjectionScanner
from dear_agent.transports.base import OutboundMessage, RawMessage
from dear_agent.transports.port import Transport

REJECT_BODIES: dict[RejectReason, str] = {
    RejectReason.ATTACHMENTS: (
        "Dear Agent does not accept source code, patches or attachments. "
        "Code travels over Git only; resend your task as plain text with a repo and "
        "instructions."
    ),
    RejectReason.NO_REPO: (
        "Dear Agent could not find a repository. Add a 'repo: <url>' line or send to an "
        "owner-repo@ routing address."
    ),
    RejectReason.EMPTY: (
        "Dear Agent could not find instructions. Describe the task in the message body."
    ),
    RejectReason.NOT_ALLOWED: (
        "Dear Agent is not allowed to work on that repository. Ask an operator to add it to "
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
    needs_human: list[str] = field(default_factory=list)


_GATE_SUMMARIES: dict[str, str] = {
    "suspicious": "message flagged as suspicious; approve to run it",
    "needs-human": "the decider says a human should approve this; approve to run it",
}


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
        human_gate: HumanGate | None = None,
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
        self._human_gate = human_gate
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
            if self._approvals is not None:
                reason = self._gate_reason(suspicious, result)
                if reason is not None:
                    self._gate_for_approval(stored, recipient, reason=reason)
                    report.gated.append(result.task.id)
                    if reason == "needs-human":
                        report.needs_human.append(result.task.id)
        self._ack(messages)
        return report

    def _gate_reason(self, suspicious: bool, result: NormalizedTask) -> str | None:
        """Why a task must be parked for a human, or ``None`` to run it (advisory)."""
        if suspicious:
            return "suspicious"
        if self._human_gate is not None and self._human_gate.needs_human(_human_state(result)):
            return "needs-human"
        return None

    def _repo_allowed(self, repo: str) -> bool:
        """A repo is allowed when an enabled project policy matches it.

        With no policies configured the deployment is open; once at least one policy exists
        it is enforced, so an unknown or disabled repo is refused — one cannot ask Dear Agent to
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

    def _gate_for_approval(
        self, task: Task, recipient: str | None, *, reason: str = "suspicious"
    ) -> None:
        """Park a task in ``action`` until a human releases it with a ``run`` approval."""
        self._queue.transition(task, TaskState.ACTION)
        self._emit(task.id, "approval.requested", reason=reason)
        summary = _GATE_SUMMARIES.get(reason, "approval requested; approve to run it")
        if self._notifier is not None and recipient:
            self._notifier.request_approval(
                task, recipient=recipient, action="run", summary=summary
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
        subject = f"[dear-agent] could not process: {message.subject or message.transport_id}"
        self._transport.send(
            OutboundMessage(
                thread_id=message.thread_id,
                subject=subject,
                body=body,
                headers={"to": recipient, "in-reply-to": message.transport_id},
            )
        )


def _human_state(result: NormalizedTask) -> str:
    """The text the human-gate decider reads: the subject plus the normalized instructions."""
    subject = result.task.subject or ""
    return "\n".join(part for part in (subject, result.spec.instructions or "") if part)


__all__ = ["ControlPlane", "IngestReport", "REJECT_BODIES"]

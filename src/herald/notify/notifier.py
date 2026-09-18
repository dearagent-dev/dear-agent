from __future__ import annotations

from dataclasses import dataclass

from herald.approvals_service import ApprovalService
from herald.queue.models import Task, TaskState
from herald.transports.base import OutboundMessage
from herald.transports.port import Transport

# Status messages carry state, a short summary and links only. Never source.
STATUS_SUBJECTS: dict[TaskState, str] = {
    TaskState.QUEUED: "queued",
    TaskState.RUNNING: "running",
    TaskState.ACTION: "needs your approval",
    TaskState.DONE: "done",
    TaskState.FAILED: "failed",
    TaskState.REJECTED: "rejected",
    TaskState.APPROVED: "approved",
}


@dataclass(slots=True)
class TaskLinks:
    """Links the operator can follow; the PR is the deliverable, not the message."""

    branch: str | None = None
    commit: str | None = None
    pr_url: str | None = None


class Notifier:
    """Sends status and approval messages over a transport.

    Outbound messages are always threaded and contain only state, summary and links. The
    notifier never sends source, patches or attachments.
    """

    def __init__(self, transport: Transport, approvals: ApprovalService | None = None) -> None:
        self._transport = transport
        self._approvals = approvals

    def status(
        self,
        task: Task,
        *,
        recipient: str,
        summary: str = "",
        links: TaskLinks | None = None,
    ) -> OutboundMessage:
        body = self._render_status(task, summary=summary, links=links)
        status = STATUS_SUBJECTS.get(task.state, task.state.value)
        subject = f"[herald] {status}: {task.subject or task.id}"
        message = OutboundMessage(
            thread_id=task.thread_id,
            subject=subject,
            body=body,
            headers=self._headers(task, recipient),
        )
        self._transport.send(message)
        return message

    def request_approval(
        self,
        task: Task,
        *,
        recipient: str,
        action: str,
        summary: str = "",
        links: TaskLinks | None = None,
    ) -> OutboundMessage:
        if self._approvals is None:
            raise RuntimeError("approval requests require an ApprovalService")
        token = self._approvals.request(task.id, action)
        body = self._render_status(task, summary=summary, links=links) + (
            f"\n\nReply 'approve {token}' or 'reject {token}' to decide.\n"
        )
        message = OutboundMessage(
            thread_id=task.thread_id,
            subject=f"[herald] approval needed: {task.subject or task.id}",
            body=body,
            headers=self._headers(task, recipient),
        )
        self._transport.send(message)
        return message

    @staticmethod
    def _headers(task: Task, recipient: str) -> dict[str, str]:
        headers = {"to": recipient, "X-Herald-Task": task.id}
        if task.thread_id is not None:
            headers["in-reply-to"] = task.transport_id
        return headers

    @staticmethod
    def _render_status(task: Task, *, summary: str, links: TaskLinks | None) -> str:
        lines = [f"task: {task.id}", f"state: {task.state.value}"]
        if summary:
            lines.append(f"summary: {summary}")
        if links is not None:
            if links.branch:
                lines.append(f"branch: {links.branch}")
            if links.commit:
                lines.append(f"commit: {links.commit}")
            if links.pr_url:
                lines.append(f"PR: {links.pr_url}")
        return "\n".join(lines)


__all__ = ["Notifier", "TaskLinks"]

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from herald.queue.models import Task
from herald.runners.worktree import RunResult
from herald.transports.base import OutboundMessage
from herald.transports.port import Transport


class FailureKind(StrEnum):
    """Why a run needs a human, mapped from the harness exit code."""

    HARNESS_MISSING = "harness_missing"
    TIMEOUT = "timeout"
    HARNESS_FAILED = "harness_failed"
    NO_CHANGES = "no_changes"


FAILURE_SUMMARIES: dict[FailureKind, str] = {
    FailureKind.HARNESS_MISSING: "the harness binary is not available on this host",
    FailureKind.TIMEOUT: "the run exceeded its time budget",
    FailureKind.HARNESS_FAILED: "the harness exited with an error",
    FailureKind.NO_CHANGES: "the harness finished but produced no changes",
}


def classify_failure(run: RunResult) -> FailureKind:
    if run.exit_code == 127:
        return FailureKind.HARNESS_MISSING
    if run.exit_code == 124:
        return FailureKind.TIMEOUT
    return FailureKind.HARNESS_FAILED


@dataclass(slots=True)
class Escalation:
    """A request for human attention when a task cannot finish on its own."""

    task_id: str
    kind: FailureKind
    message: str


class Escalator:
    """Builds and sends failure/escalation messages.

    The message asks a human to look, names the failure kind, and links nothing that would
    carry source. Raw harness output stays out of the transport.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def escalate(
        self,
        task: Task,
        run: RunResult,
        *,
        recipient: str,
        branch: str | None = None,
    ) -> Escalation:
        kind = classify_failure(run)
        escalation = Escalation(
            task_id=task.id,
            kind=kind,
            message=FAILURE_SUMMARIES[kind],
        )
        lines = [
            f"task: {task.id}",
            "state: needs attention",
            f"reason: {FAILURE_SUMMARIES[kind]} (exit {run.exit_code})",
        ]
        if branch:
            lines.append(f"branch: {branch}")
        lines.append("A human should review this run before it is retried or closed.")
        self._transport.send(
            OutboundMessage(
                thread_id=task.thread_id,
                subject=f"[herald] needs attention: {task.subject or task.id}",
                body="\n".join(lines),
                headers={"to": recipient, "X-Herald-Task": task.id},
            )
        )
        return escalation


__all__ = ["Escalation", "Escalator", "FAILURE_SUMMARIES", "FailureKind", "classify_failure"]

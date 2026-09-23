from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from herald.queue.models import Task
from herald.runners.worktree import RunResult
from herald.transports.base import OutboundMessage
from herald.transports.port import Transport


class FailureKind(StrEnum):
    """Why a run needs a human, mapped from the harness exit code or the publish step."""

    HARNESS_MISSING = "harness_missing"
    TIMEOUT = "timeout"
    HARNESS_FAILED = "harness_failed"
    NO_CHANGES = "no_changes"
    PUBLISH_FAILED = "publish_failed"
    VERIFY_FAILED = "verify_failed"
    VERIFY_BLOCKED = "verify_blocked"


FAILURE_SUMMARIES: dict[FailureKind, str] = {
    FailureKind.HARNESS_MISSING: "the harness binary is not available on this host",
    FailureKind.TIMEOUT: "the run exceeded its time budget",
    FailureKind.HARNESS_FAILED: "the harness exited with an error",
    FailureKind.NO_CHANGES: "the harness finished but produced no changes",
    FailureKind.PUBLISH_FAILED: "the change could not be pushed or the draft PR opened",
    FailureKind.VERIFY_FAILED: "the change did not pass the task's verify command",
    FailureKind.VERIFY_BLOCKED: "the task's verify command is not on the allowlist",
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
        kind: FailureKind | None = None,
        detail: str | None = None,
    ) -> Escalation:
        # The caller may already know the kind (e.g. NO_CHANGES or PUBLISH_FAILED, which the
        # exit code cannot express); otherwise classify from the exit code.
        kind = kind or classify_failure(run)
        escalation = Escalation(
            task_id=task.id,
            kind=kind,
            message=FAILURE_SUMMARIES[kind],
        )
        lines = [f"task: {task.id}", "state: needs attention"]
        if kind in {FailureKind.HARNESS_MISSING, FailureKind.TIMEOUT, FailureKind.HARNESS_FAILED}:
            lines.append(f"reason: {FAILURE_SUMMARIES[kind]} (exit {run.exit_code})")
        else:
            lines.append(f"reason: {FAILURE_SUMMARIES[kind]}")
        if detail:
            lines.append(f"detail: {detail}")
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

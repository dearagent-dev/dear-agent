from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from herald.events import ErrorBudget, EventLog
from herald.executor import DEFAULT_LEASE, ExecutedTask, TaskExecutor
from herald.queue.models import Task, TaskSpec, TaskState
from herald.queue.port import Queue

SpecResolver = Callable[[Task, str], TaskSpec]

DEFAULT_MAX_ATTEMPTS = 3


class TaskWorkerError(RuntimeError):
    """A task could not be resolved or executed."""


@dataclass(slots=True)
class TaskWorker:
    """Executes one claimed task end to end.

    This is the entrypoint a runner Job invokes: given a task id (or the next queued task),
    it loads the task, resolves its content into a :class:`TaskSpec` and hands it to the
    :class:`TaskExecutor`. The spec is normally persisted with the task; the resolver only
    falls back to the mailbox for legacy records (ADR 0005).
    """

    queue: Queue
    executor: TaskExecutor
    resolve_spec: SpecResolver
    repo_path: str
    recipient: str | None = None
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    lease: timedelta = DEFAULT_LEASE
    events: EventLog | None = None
    budget: ErrorBudget = field(default_factory=ErrorBudget)

    def run(self, task_id: str) -> ExecutedTask:
        task = self.queue.get(task_id)
        if task is None:
            raise TaskWorkerError(f"no task {task_id!r} in the queue")
        self._guard_budget(task.id)
        self._guard_attempts(task)
        spec = self.resolve_spec(task, self.repo_path)
        return self.executor.execute(task, spec, recipient=self.recipient)

    def run_next(self) -> ExecutedTask:
        """Atomically claim the oldest queued task and run it (a local sweep tick)."""
        self._guard_budget("<dispatch>")
        task = self.queue.claim_next(lease=self.lease)
        if task is None:
            raise TaskWorkerError("no queued task to run")
        self._guard_attempts(task)
        spec = self.resolve_spec(task, self.repo_path)
        return self.executor.execute(task, spec, recipient=self.recipient, claimed=True)

    def _guard_budget(self, task_id: str) -> None:
        # A durable circuit breaker: if too many tasks failed recently, stop starting new
        # runs until the window clears, rather than grinding through a systemic failure.
        if self.events is None or not self.budget.exhausted(self.events):
            return
        with contextlib.suppress(Exception):
            self.events.record(task_id, "budget.exhausted")
        raise TaskWorkerError(
            f"error budget exhausted ({self.budget.max_failures} failures in "
            f"{self.budget.window_seconds}s); not starting new runs"
        )

    def _guard_attempts(self, task: Task) -> None:
        # A task that keeps being released by the lease sweep (crash loop) must stop and ask
        # a human rather than retry forever.
        if task.attempts >= self.max_attempts:
            self.queue.transition(task, TaskState.FAILED)
            raise TaskWorkerError(
                f"task {task.id!r} exceeded {self.max_attempts} attempts; left failed"
            )


__all__ = ["DEFAULT_MAX_ATTEMPTS", "SpecResolver", "TaskWorker", "TaskWorkerError"]

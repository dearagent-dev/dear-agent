from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from herald.executor import ExecutedTask, TaskExecutor
from herald.queue.models import Task, TaskSpec, TaskState
from herald.queue.port import Queue

SpecResolver = Callable[[Task, str], TaskSpec]

DEFAULT_MAX_ATTEMPTS = 3


class TaskWorkerError(RuntimeError):
    """A task could not be resolved or executed."""


@dataclass(slots=True)
class TaskWorker:
    """Executes one claimed task end to end.

    This is the entrypoint a runner Job invokes: given a task id, it loads the task,
    resolves its content into a :class:`TaskSpec`, and hands it to the :class:`TaskExecutor`.
    The resolver is injected because the message body lives in the mailbox, not in the queue
    record (see ADR 0002).
    """

    queue: Queue
    executor: TaskExecutor
    resolve_spec: SpecResolver
    repo_path: str
    recipient: str | None = None
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    def run(self, task_id: str) -> ExecutedTask:
        task = self.queue.get(task_id)
        if task is None:
            raise TaskWorkerError(f"no task {task_id!r} in the queue")
        # A task that keeps being released by the lease sweep (crash loop) must stop and ask
        # a human rather than retry forever.
        if task.attempts >= self.max_attempts:
            self.queue.transition(task, TaskState.FAILED)
            raise TaskWorkerError(
                f"task {task_id!r} exceeded {self.max_attempts} attempts; left failed"
            )
        spec = self.resolve_spec(task, self.repo_path)
        return self.executor.execute(task, spec, recipient=self.recipient)


__all__ = ["SpecResolver", "TaskWorker", "TaskWorkerError"]

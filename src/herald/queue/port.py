from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from herald.queue.models import Task, TaskState


class QueueError(Exception):
    """Base class for queue errors."""


class TaskNotFoundError(QueueError):
    """Raised when a task id does not exist in the queue."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"no task with id {task_id!r}")
        self.task_id = task_id


class StateConflictError(QueueError):
    """Raised when a guarded transition sees a different state than expected."""

    def __init__(self, task_id: str, expected: TaskState, actual: TaskState) -> None:
        super().__init__(f"task {task_id!r} is {actual.value}, expected {expected.value}")
        self.task_id = task_id
        self.expected = expected
        self.actual = actual


@runtime_checkable
class Queue(Protocol):
    """Durable task queue.

    The authoritative store is the transport mailbox (Fastmail JMAP first): state lives in
    mailboxes/keywords and transitions are guarded by ``ifInState``. Implementations must
    preserve the atomic-claim and dedupe guarantees regardless of backend.
    """

    def enqueue(self, task: Task) -> Task | None:
        """Store ``task`` in ``Queued``.

        Return ``None`` when a task with the same ``transport_id`` already exists, so a
        redelivered message is a no-op.
        """
        ...

    def get(self, task_id: str) -> Task | None:
        """Return the task with ``task_id``, or ``None`` if it does not exist."""
        ...

    def list(self, state: TaskState, *, limit: int = 10) -> list[Task]:
        """Return tasks in ``state``, oldest first."""
        ...

    def claim(self, task: Task, *, lease: timedelta) -> bool:
        """Atomically move ``task`` from ``Queued`` to ``Running`` with a lease.

        Return ``False`` if another worker claimed it first or it is not queued.
        """
        ...

    def transition(self, task: Task, to_state: TaskState) -> Task:
        """Move ``task`` to ``to_state`` if the queue still holds ``task.state``.

        Raise :class:`StateConflictError` on a concurrent change and
        :class:`TaskNotFoundError` when the task no longer exists.
        """
        ...

    def release_stale(self, *, now: datetime) -> list[Task]:
        """Return expired ``Running`` tasks to ``Queued`` and bump their attempts."""
        ...

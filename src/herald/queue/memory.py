from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import datetime, timedelta

from herald.queue.models import Evidence, Task, TaskState, utcnow
from herald.queue.port import StateConflictError, TaskNotFoundError


class MemoryQueue:
    """In-memory :class:`~herald.queue.port.Queue` for tests and offline development.

    Task records are copied on the way in and out so callers cannot mutate stored state.
    """

    def __init__(self, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._clock = clock
        self._tasks: dict[str, Task] = {}
        self._by_transport: dict[str, str] = {}

    def enqueue(self, task: Task) -> Task | None:
        # Dedupe on either key, so a task whose id and transport_id diverge cannot clobber
        # an existing record or leave the transport index stale.
        if task.transport_id in self._by_transport or task.id in self._tasks:
            return None

        now = self._clock()
        stored = copy.deepcopy(task)
        stored.state = TaskState.QUEUED
        stored.attempts = 0
        stored.lease_until = None
        stored.created_at = now

        self._tasks[stored.id] = stored
        self._by_transport[stored.transport_id] = stored.id
        return copy.deepcopy(stored)

    def get(self, task_id: str) -> Task | None:
        stored = self._tasks.get(task_id)
        return copy.deepcopy(stored) if stored is not None else None

    def list(self, state: TaskState, *, limit: int = 10) -> list[Task]:
        tasks = [task for task in self._tasks.values() if task.state == state]
        tasks.sort(key=lambda task: (task.created_at, task.id))
        return [copy.deepcopy(task) for task in tasks[:limit]]

    def claim(self, task: Task, *, lease: timedelta) -> bool:
        stored = self._tasks.get(task.id)
        if stored is None or stored.state != TaskState.QUEUED:
            return False

        now = self._clock()
        stored.state = TaskState.RUNNING
        stored.lease_until = now + lease
        return True

    def claim_next(self, *, lease: timedelta) -> Task | None:
        queued = [task for task in self._tasks.values() if task.state == TaskState.QUEUED]
        if not queued:
            return None
        stored = min(queued, key=lambda task: (task.created_at, task.id))
        stored.state = TaskState.RUNNING
        stored.lease_until = self._clock() + lease
        return copy.deepcopy(stored)

    def transition(
        self,
        task: Task,
        to_state: TaskState,
        *,
        evidence: Evidence | None = None,
    ) -> Task:
        stored = self._tasks.get(task.id)
        if stored is None:
            raise TaskNotFoundError(task.id)
        if stored.state != task.state:
            raise StateConflictError(task.id, task.state, stored.state)

        stored.state = to_state
        if to_state != TaskState.RUNNING:
            stored.lease_until = None
        if evidence is not None:
            stored.evidence = evidence
        return copy.deepcopy(stored)

    def release_stale(self, *, now: datetime) -> list[Task]:
        released: list[Task] = []
        for stored in self._tasks.values():
            if stored.state != TaskState.RUNNING:
                continue
            if stored.lease_until is None or stored.lease_until >= now:
                continue

            stored.state = TaskState.QUEUED
            stored.attempts += 1
            stored.lease_until = None
            released.append(copy.deepcopy(stored))

        released.sort(key=lambda task: (task.created_at, task.id))
        return released

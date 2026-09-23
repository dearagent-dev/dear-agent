from __future__ import annotations

from dataclasses import dataclass, field

from herald.queue.models import Task, TaskState
from herald.queue.port import Queue

_FAILED_STATES = {TaskState.FAILED, TaskState.REJECTED}
_READY = "ready"
_BLOCKED = "blocked"
_FAILED = "failed"


@dataclass(slots=True, frozen=True)
class DependencyStatus:
    """Whether a task's declared dependencies allow it to run now."""

    state: str = _READY
    blocked_on: tuple[str, ...] = field(default_factory=tuple)
    failed_on: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        return self.state == _READY


def dependency_status(queue: Queue, task: Task) -> DependencyStatus:
    """Check a task's ``depends_on`` ids against the queue.

    A dependency that is ``done`` is satisfied; one that is ``failed``/``rejected`` (or
    cannot be found) blocks the task permanently; anything else (queued, running, action)
    means the task is simply not ready yet. A task with no dependencies is always ready.
    """
    depends_on = task.spec.depends_on if task.spec else ()
    if not depends_on:
        return DependencyStatus()

    blocked: list[str] = []
    failed: list[str] = []
    for dep_id in depends_on:
        dependency = queue.get(dep_id)
        if dependency is None:
            # Unknown: it may not be enqueued yet, so treat it as not-ready rather than
            # failing the task outright.
            blocked.append(dep_id)
        elif dependency.state is TaskState.DONE:
            continue
        elif dependency.state in _FAILED_STATES:
            failed.append(dep_id)
        else:
            blocked.append(dep_id)

    if failed:
        return DependencyStatus(state=_FAILED, blocked_on=tuple(blocked), failed_on=tuple(failed))
    if blocked:
        return DependencyStatus(state=_BLOCKED, blocked_on=tuple(blocked))
    return DependencyStatus()


__all__ = ["DependencyStatus", "dependency_status"]

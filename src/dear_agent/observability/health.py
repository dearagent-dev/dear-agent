from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from dear_agent.queue.models import TaskState
from dear_agent.queue.port import Queue

TERMINAL_STATES = {TaskState.DONE, TaskState.FAILED, TaskState.REJECTED, TaskState.APPROVED}
COUNTED_STATES = (
    TaskState.QUEUED,
    TaskState.RUNNING,
    TaskState.ACTION,
    TaskState.DONE,
    TaskState.FAILED,
    TaskState.REJECTED,
)


@dataclass(slots=True)
class Metrics:
    """In-process task counters.

    Deliberately dependency-free: a dict of counters is easy to expose and to test. A
    Prometheus exporter can read :meth:`snapshot` later without changing callers.
    """

    counters: Counter[str] = field(default_factory=Counter)

    def incr(self, name: str, amount: int = 1) -> None:
        self.counters[name] += amount

    def reset(self) -> None:
        self.counters.clear()

    def snapshot(self) -> dict[str, int]:
        return dict(self.counters)


@dataclass(slots=True)
class HealthStatus:
    """A point-in-time health view for a ``/health`` endpoint."""

    ok: bool
    checked_at: datetime
    counts: dict[str, int] = field(default_factory=dict)
    details: dict[str, int] = field(default_factory=dict)

    @property
    def queue_depth(self) -> int:
        return self.counts.get(TaskState.QUEUED.value, 0)

    @property
    def running(self) -> int:
        return self.counts.get(TaskState.RUNNING.value, 0)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at.isoformat(),
            "counts": self.counts,
            **self.details,
        }


class Health:
    """Builds a health status from the queue.

    Healthy means the control plane can read its queue and is not running more tasks than
    its limit. A non-empty queue is **not** unhealthy: work is asynchronous by design.
    """

    def __init__(self, *, max_running: int = 1, stall_margin_seconds: int = 300) -> None:
        self._max_running = max_running
        self._stall_margin = timedelta(seconds=stall_margin_seconds)

    def check(self, queue: Queue, *, now: datetime | None = None) -> HealthStatus:
        moment = now or datetime.now(UTC)
        counts = {state.value: len(queue.list(state, limit=1000)) for state in COUNTED_STATES}
        running = counts[TaskState.RUNNING.value]
        # A soft stall watchdog: a running task whose lease is about to expire (or already
        # has) may be wedged. The sweep requeues it; this surfaces it before it goes silent.
        stalled = sum(
            1
            for task in queue.list(TaskState.RUNNING, limit=1000)
            if task.lease_until is not None and task.lease_until <= moment + self._stall_margin
        )
        return HealthStatus(
            ok=running <= self._max_running,
            checked_at=moment,
            counts=counts,
            details={"max_running": self._max_running, "stalled": stalled},
        )


__all__ = ["COUNTED_STATES", "Health", "HealthStatus", "Metrics", "TERMINAL_STATES"]

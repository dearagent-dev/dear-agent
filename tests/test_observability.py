from __future__ import annotations

from datetime import UTC, datetime, timedelta

from herald.observability.health import Health, Metrics
from herald.queue.memory import MemoryQueue
from herald.queue.models import Task, TaskState


def enqueue(queue: MemoryQueue, task_id: str) -> Task:
    stored = queue.enqueue(Task(id=task_id, transport_id=f"<{task_id}@x>"))
    assert stored is not None
    return stored


def test_metrics_count_and_snapshot() -> None:
    metrics = Metrics()

    metrics.incr("tasks.accepted")
    metrics.incr("tasks.accepted")
    metrics.incr("tasks.rejected", 3)

    assert metrics.snapshot() == {"tasks.accepted": 2, "tasks.rejected": 3}


def test_metrics_reset() -> None:
    metrics = Metrics()
    metrics.incr("x")

    metrics.reset()

    assert metrics.snapshot() == {}


def test_health_is_ok_with_an_empty_queue() -> None:
    status = Health().check(MemoryQueue())

    assert status.ok is True
    assert status.queue_depth == 0
    assert status.running == 0


def test_health_counts_states() -> None:
    queue = MemoryQueue()
    enqueue(queue, "e1")
    enqueue(queue, "e2")

    status = Health().check(queue)

    assert status.counts[TaskState.QUEUED.value] == 2


def test_a_nonempty_queue_is_still_healthy() -> None:
    queue = MemoryQueue()
    enqueue(queue, "e1")

    assert Health().check(queue).ok is True


def test_too_many_running_is_unhealthy() -> None:
    queue = MemoryQueue()
    first = enqueue(queue, "e1")
    second = enqueue(queue, "e2")
    queue.claim(first, lease=timedelta(hours=1))
    queue.claim(second, lease=timedelta(hours=1))

    status = Health(max_running=1).check(queue)

    assert status.ok is False
    assert status.running == 2


def test_health_status_serializes() -> None:
    status = Health().check(MemoryQueue(), now=datetime(2026, 1, 1, tzinfo=UTC))

    payload = status.to_dict()

    assert payload["ok"] is True
    assert payload["checked_at"] == "2026-01-01T00:00:00+00:00"

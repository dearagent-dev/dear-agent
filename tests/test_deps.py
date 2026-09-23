from __future__ import annotations

from datetime import timedelta

from dear_agent.deps import dependency_status
from dear_agent.queue.memory import MemoryQueue
from dear_agent.queue.models import Task, TaskSpec, TaskState


def make(queue: MemoryQueue, task_id: str, *, depends_on: tuple[str, ...] = ()) -> Task:
    stored = queue.enqueue(
        Task(
            id=task_id,
            transport_id=f"<{task_id}@x>",
            spec=TaskSpec(repo_url="r", depends_on=depends_on),
        )
    )
    assert stored is not None
    return stored


def finish(queue: MemoryQueue, task_id: str, state: TaskState) -> None:
    task = queue.get(task_id)
    assert task is not None
    queue.claim(task, lease=timedelta(hours=1))
    queue.transition(queue.get(task_id), state)


def test_no_dependencies_is_ready() -> None:
    queue = MemoryQueue()
    task = make(queue, "e1")

    assert dependency_status(queue, task).ready is True


def test_a_done_dependency_is_satisfied() -> None:
    queue = MemoryQueue()
    make(queue, "d1")
    finish(queue, "d1", TaskState.DONE)
    task = make(queue, "e1", depends_on=("d1",))

    assert dependency_status(queue, task).ready is True


def test_a_pending_dependency_blocks() -> None:
    queue = MemoryQueue()
    make(queue, "d1")
    task = make(queue, "e1", depends_on=("d1",))

    status = dependency_status(queue, task)

    assert status.state == "blocked"
    assert status.blocked_on == ("d1",)


def test_a_failed_dependency_fails_the_status() -> None:
    queue = MemoryQueue()
    make(queue, "d1")
    finish(queue, "d1", TaskState.FAILED)
    task = make(queue, "e1", depends_on=("d1",))

    status = dependency_status(queue, task)

    assert status.state == "failed"
    assert status.failed_on == ("d1",)


def test_an_unknown_dependency_blocks() -> None:
    queue = MemoryQueue()
    task = make(queue, "e1", depends_on=("missing",))

    assert dependency_status(queue, task).state == "blocked"

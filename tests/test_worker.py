from __future__ import annotations

import pytest

from herald.executor import ExecutedTask
from herald.queue.memory import MemoryQueue
from herald.queue.models import Task, TaskSpec
from herald.runners.worktree import RunResult
from herald.worker import TaskWorker, TaskWorkerError


class CapturingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, TaskSpec, str | None]] = []

    def execute(self, task: Task, spec: TaskSpec, *, recipient: str | None = None) -> ExecutedTask:
        self.calls.append((task.id, spec, recipient))
        return ExecutedTask(
            task_id=task.id,
            branch="herald/add-healthz",
            commit="abc",
            pr_url="https://example.com/pr/1",
            run=RunResult(exit_code=0, branch="herald/add-healthz"),
        )


def test_worker_resolves_and_executes() -> None:
    queue = MemoryQueue()
    queue.enqueue(Task(id="e1", transport_id="<m1@x>", subject="add healthz"))
    spec = TaskSpec(repo_url="https://example.com/o/r", instructions="add /healthz")
    executor = CapturingExecutor()

    worker = TaskWorker(
        queue=queue,
        executor=executor,  # type: ignore[arg-type]
        resolve_spec=lambda task, repo: spec,
        repo_path="/repos/source",
        recipient="dev@example.com",
    )

    evidence = worker.run("e1")

    assert executor.calls == [("e1", spec, "dev@example.com")]
    assert evidence.pr_url == "https://example.com/pr/1"


def test_worker_rejects_an_unknown_task() -> None:
    worker = TaskWorker(
        queue=MemoryQueue(),
        executor=CapturingExecutor(),  # type: ignore[arg-type]
        resolve_spec=lambda task, repo: TaskSpec(repo_url=repo),
        repo_path="/repos/source",
    )

    with pytest.raises(TaskWorkerError):
        worker.run("missing")

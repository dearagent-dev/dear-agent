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
        self.claimed = False

    def execute(
        self,
        task: Task,
        spec: TaskSpec,
        *,
        recipient: str | None = None,
        claimed: bool = False,
    ) -> ExecutedTask:
        self.claimed = claimed
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


def test_worker_run_next_claims_the_oldest_without_double_claiming() -> None:
    from datetime import UTC, datetime, timedelta

    from herald.queue.models import TaskState

    class Clock:
        def __init__(self) -> None:
            self.now = datetime(2026, 1, 1, tzinfo=UTC)

        def __call__(self):
            return self.now

    clock = Clock()
    queue = MemoryQueue(clock=clock)
    queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    clock.now += timedelta(minutes=1)
    queue.enqueue(Task(id="e2", transport_id="<m2@x>"))
    executor = CapturingExecutor()
    worker = TaskWorker(
        queue=queue,
        executor=executor,  # type: ignore[arg-type]
        resolve_spec=lambda task, repo: TaskSpec(repo_url="x"),
        repo_path=".",
    )

    evidence = worker.run_next()

    assert evidence.task_id == "e1"
    assert executor.claimed is True
    assert queue.get("e1").state is TaskState.RUNNING
    assert queue.get("e2").state is TaskState.QUEUED


def test_worker_run_next_raises_when_nothing_is_queued() -> None:
    worker = TaskWorker(
        queue=MemoryQueue(),
        executor=CapturingExecutor(),  # type: ignore[arg-type]
        resolve_spec=lambda task, repo: TaskSpec(repo_url="x"),
        repo_path=".",
    )

    with pytest.raises(TaskWorkerError):
        worker.run_next()


def test_worker_rejects_an_unknown_task() -> None:
    worker = TaskWorker(
        queue=MemoryQueue(),
        executor=CapturingExecutor(),  # type: ignore[arg-type]
        resolve_spec=lambda task, repo: TaskSpec(repo_url=repo),
        repo_path="/repos/source",
    )

    with pytest.raises(TaskWorkerError):
        worker.run("missing")


def test_build_transport_honors_the_backend_argument(monkeypatch) -> None:
    from herald.transports.memory import MemoryTransport
    from herald.worker_factory import build_transport

    monkeypatch.setenv("HERALD_BACKEND", "memory")

    assert isinstance(build_transport("memory"), MemoryTransport)


def test_build_transport_prefers_the_backend_argument_over_env(monkeypatch) -> None:
    from unittest import mock

    from herald.worker_factory import build_transport

    monkeypatch.setenv("HERALD_BACKEND", "memory")
    monkeypatch.setenv("FASTMAIL_API_TOKEN", "token")

    with (
        mock.patch("herald.jmap.client.JmapClient.connect"),
        mock.patch("herald.jmap.client.JmapClient.__init__", return_value=None),
    ):
        transport = build_transport("jmap")

    assert type(transport).__name__ == "JmapTransport"


def test_build_runner_selects_the_command_harness(monkeypatch) -> None:
    from herald.runners.command import CommandRunner
    from herald.worker_factory import build_runner

    monkeypatch.setenv("HERALD_HARNESS", "command")
    monkeypatch.setenv("HERALD_HARNESS_COMMAND", "my-agent --flag")

    runner = build_runner("model", sandbox=object())  # type: ignore[arg-type]

    assert isinstance(runner, CommandRunner)
    assert runner.command == ["my-agent", "--flag"]


def test_build_runner_command_requires_the_command(monkeypatch) -> None:
    from herald.worker_factory import build_runner

    monkeypatch.setenv("HERALD_HARNESS", "command")
    monkeypatch.delenv("HERALD_HARNESS_COMMAND", raising=False)

    with __import__("pytest").raises(RuntimeError):
        build_runner(None, sandbox=object())  # type: ignore[arg-type]


def test_build_runner_selects_claude_and_codex(monkeypatch) -> None:
    from herald.runners.claude import ClaudeCodeRunner
    from herald.runners.codex import CodexRunner
    from herald.worker_factory import build_runner

    monkeypatch.setenv("HERALD_HARNESS", "claude")
    assert isinstance(build_runner(None, sandbox=object()), ClaudeCodeRunner)  # type: ignore[arg-type]

    monkeypatch.setenv("HERALD_HARNESS", "codex")
    assert isinstance(build_runner(None, sandbox=object()), CodexRunner)  # type: ignore[arg-type]


def test_build_runner_defaults_to_opencode(monkeypatch) -> None:
    from herald.runners.opencode import OpenCodeRunner
    from herald.worker_factory import build_runner

    monkeypatch.delenv("HERALD_HARNESS", raising=False)
    assert isinstance(build_runner(None, sandbox=object()), OpenCodeRunner)  # type: ignore[arg-type]


def test_build_routing_runner_is_none_without_config(monkeypatch) -> None:
    from herald.worker_factory import build_routing_runner

    monkeypatch.delenv("HERALD_HARNESSES", raising=False)
    assert build_routing_runner(sandbox=object()) is None  # type: ignore[arg-type]


def test_build_routing_runner_maps_classes(monkeypatch) -> None:
    from herald.runners.routing import RoutingRunner
    from herald.worker_factory import build_routing_runner

    monkeypatch.setenv("HERALD_HARNESSES", "local:opencode,hosted:command")
    monkeypatch.setenv("HERALD_HARNESS_COMMAND", "my-agent")
    monkeypatch.setenv("HERALD_DECIDER", "rules")

    runner = build_routing_runner(sandbox=object())  # type: ignore[arg-type]

    assert isinstance(runner, RoutingRunner)
    assert set(runner.runners) == {"local", "hosted"}
    assert runner.default == "hosted"


def test_worker_stops_when_the_error_budget_is_exhausted() -> None:
    from herald.events import ErrorBudget, MemoryEventLog
    from herald.queue.models import TaskState

    log = MemoryEventLog()
    log.record("f1", "task.failed")
    log.record("f2", "task.failed")
    queue = MemoryQueue()
    queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    worker = TaskWorker(
        queue=queue,
        executor=CapturingExecutor(),  # type: ignore[arg-type]
        resolve_spec=lambda task, repo: TaskSpec(repo_url="x"),
        repo_path=".",
        events=log,
        budget=ErrorBudget(max_failures=2),
    )

    with pytest.raises(TaskWorkerError):
        worker.run("e1")

    assert queue.get("e1").state is TaskState.QUEUED  # never claimed


def test_worker_fails_a_task_after_too_many_attempts() -> None:
    from datetime import UTC, datetime, timedelta

    from herald.queue.models import TaskState

    class Clock:
        def __init__(self) -> None:
            self.now = datetime(2026, 1, 1, tzinfo=UTC)

        def __call__(self):
            return self.now

    clock = Clock()
    queue = MemoryQueue(clock=clock)
    task = queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    assert task is not None
    for _ in range(2):
        queue.claim(task, lease=timedelta(seconds=1))
        clock.now += timedelta(seconds=2)
        queue.release_stale(now=clock.now)

    worker = TaskWorker(
        queue=queue,
        executor=object(),  # type: ignore[arg-type]
        resolve_spec=lambda task, path: TaskSpec(repo_url="x"),  # type: ignore[arg-type]
        repo_path=".",
        max_attempts=2,
    )

    with pytest.raises(TaskWorkerError):
        worker.run("e1")

    assert queue.get("e1").state is TaskState.FAILED


def test_worker_refuses_a_blocked_task() -> None:
    from herald.queue.models import TaskState

    queue = MemoryQueue()
    queue.enqueue(Task(id="d1", transport_id="<d1@x>"))
    queue.enqueue(
        Task(
            id="e1",
            transport_id="<e1@x>",
            spec=TaskSpec(repo_url="r", depends_on=("d1",)),
        )
    )
    worker = TaskWorker(
        queue=queue,
        executor=CapturingExecutor(),  # type: ignore[arg-type]
        resolve_spec=lambda task, repo: TaskSpec(repo_url="x"),
        repo_path=".",
    )

    with pytest.raises(TaskWorkerError):
        worker.run("e1")

    assert queue.get("e1").state is TaskState.QUEUED


def test_run_next_skips_a_blocked_task_and_runs_the_ready_one() -> None:
    from datetime import timedelta

    from herald.queue.models import TaskState

    queue = MemoryQueue()
    dependency = queue.enqueue(Task(id="d1", transport_id="<d1@x>"))
    assert dependency is not None
    queue.claim(dependency, lease=timedelta(hours=1))  # running, so not in the queued list
    queue.enqueue(
        Task(id="e1", transport_id="<e1@x>", spec=TaskSpec(repo_url="r", depends_on=("d1",)))
    )
    queue.enqueue(Task(id="e2", transport_id="<e2@x>"))
    worker = TaskWorker(
        queue=queue,
        executor=CapturingExecutor(),  # type: ignore[arg-type]
        resolve_spec=lambda task, repo: TaskSpec(repo_url="x"),
        repo_path=".",
    )

    evidence = worker.run_next()

    assert evidence.task_id == "e2"
    assert queue.get("e1").state is TaskState.QUEUED


def test_run_next_fails_a_task_whose_dependency_failed() -> None:
    from datetime import timedelta

    from herald.queue.models import TaskState

    queue = MemoryQueue()
    dependency = queue.enqueue(Task(id="d1", transport_id="<d1@x>"))
    assert dependency is not None
    queue.claim(dependency, lease=timedelta(hours=1))
    queue.transition(queue.get("d1"), TaskState.FAILED)
    queue.enqueue(
        Task(id="e1", transport_id="<e1@x>", spec=TaskSpec(repo_url="r", depends_on=("d1",)))
    )
    worker = TaskWorker(
        queue=queue,
        executor=CapturingExecutor(),  # type: ignore[arg-type]
        resolve_spec=lambda task, repo: TaskSpec(repo_url="x"),
        repo_path=".",
    )

    with pytest.raises(TaskWorkerError):
        worker.run_next()

    assert queue.get("e1").state is TaskState.FAILED


def test_spec_resolver_prefers_the_persisted_spec() -> None:
    from herald.worker_factory import WorktreeSpecResolver

    class ExplodingTransport:
        def poll(self):
            raise AssertionError("must not poll the transport when the spec is persisted")

    class RecordingPreparer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def ensure(self, path: str, url: str) -> None:
            self.calls.append((path, url))

    preparer = RecordingPreparer()
    resolver = WorktreeSpecResolver(ExplodingTransport(), preparer=preparer)
    spec = TaskSpec(repo_url="git@github.com:o/r.git", instructions="do it")
    task = Task(id="e1", transport_id="<m1@x>", spec=spec)

    assert resolver(task, "/work/source") == spec
    assert preparer.calls == [("/work/source", "git@github.com:o/r.git")]

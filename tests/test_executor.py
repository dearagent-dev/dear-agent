from __future__ import annotations

import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from herald.executor import TaskExecutor, _slug
from herald.gitplane.plane import GitPlane, PullRequest
from herald.notify.escalate import Escalator, FailureKind
from herald.notify.notifier import Notifier
from herald.queue.memory import MemoryQueue
from herald.queue.models import Task, TaskSpec, TaskState
from herald.runners.worktree import RunResult, Worktree
from herald.transports.memory import MemoryTransport


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


class RecordingForge:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def open_draft_pr(
        self, *, repo_path: Path, branch: str, base_branch: str, title: str, body: str
    ) -> PullRequest:
        self.calls.append({"branch": branch, "base_branch": base_branch, "title": title})
        return PullRequest(url="https://example.com/pr/1", number=1)


class FakeRunner:
    def __init__(self, ok: bool = True, write: bool = True) -> None:
        self.ok = ok
        self.write = write
        self.ran_in: Path | None = None

    def run(self, task: Task, spec: TaskSpec, worktree: Worktree) -> RunResult:
        self.ran_in = worktree.path
        if self.ok and self.write:
            (worktree.path / "change.txt").write_text("done\n")
        return RunResult(
            exit_code=0 if self.ok else 1, stdout="out", stderr="err", branch=worktree.branch
        )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    path = tmp_path / "source"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "initial")
    git(path, "remote", "add", "origin", str(remote))
    git(path, "push", "-u", "origin", "main")
    return path


def make_task(queue: MemoryQueue, subject: str = "add health endpoint") -> Task:
    stored = queue.enqueue(Task(id="e1", transport_id="<m1@x>", thread_id="t1", subject=subject))
    assert stored is not None
    return stored


def make_spec() -> TaskSpec:
    return TaskSpec(repo_url="https://example.com/o/r", instructions="add /healthz")


def make_executor(
    queue: MemoryQueue,
    repo: Path,
    forge: RecordingForge,
    runner: FakeRunner,
    transport: MemoryTransport,
) -> TaskExecutor:
    return TaskExecutor(
        queue=queue,
        runner=runner,
        git=GitPlane(forge=forge),
        repo_path=str(repo),
        worktrees_root=str(repo.parent / "wt"),
        notifier=Notifier(transport),
        escalator=Escalator(transport),
    )


def test_successful_run_marks_done_and_opens_a_draft_pr(repo: Path) -> None:
    queue = MemoryQueue()
    task = make_task(queue)
    forge = RecordingForge()
    transport = MemoryTransport()
    executor = make_executor(queue, repo, forge, FakeRunner(ok=True), transport)

    evidence = executor.execute(task, make_spec(), recipient="dev@example.com")

    assert queue.get("e1").state is TaskState.DONE
    assert evidence.commit is not None
    assert evidence.pr_url == "https://example.com/pr/1"
    assert forge.calls[0]["branch"].startswith("herald/")
    assert transport.outbox[0].body.find("PR: https://example.com/pr/1") != -1


def test_failed_harness_marks_failed_and_opens_no_pr(repo: Path) -> None:
    queue = MemoryQueue()
    task = make_task(queue)
    forge = RecordingForge()
    transport = MemoryTransport()
    executor = make_executor(queue, repo, forge, FakeRunner(ok=False), transport)

    evidence = executor.execute(task, make_spec(), recipient="dev@example.com")

    assert queue.get("e1").state is TaskState.FAILED
    assert evidence.commit is None
    assert evidence.pr_url is None
    assert forge.calls == []


def test_worktree_is_cleaned_up_after_a_run(repo: Path) -> None:
    queue = MemoryQueue()
    task = make_task(queue)
    executor = make_executor(queue, repo, RecordingForge(), FakeRunner(), MemoryTransport())

    executor.execute(task, make_spec())

    assert list((repo.parent / "wt").glob("*")) == []


def test_run_happens_in_an_isolated_worktree_not_the_source(repo: Path) -> None:
    queue = MemoryQueue()
    task = make_task(queue)
    runner = FakeRunner()
    executor = make_executor(queue, repo, RecordingForge(), runner, MemoryTransport())

    executor.execute(task, make_spec())

    assert runner.ran_in is not None
    assert runner.ran_in != repo
    assert not (repo / "change.txt").exists()


def test_execute_rejects_a_task_that_is_not_queued(repo: Path) -> None:
    queue = MemoryQueue()
    task = make_task(queue)
    queue.claim(task, lease=timedelta(hours=1))
    executor = make_executor(queue, repo, RecordingForge(), FakeRunner(), MemoryTransport())

    with pytest.raises(RuntimeError):
        executor.execute(task, make_spec())


def test_slug_is_branch_safe() -> None:
    task = Task(id="e1", transport_id="<m1@x>", subject="Add /healthz & metrics!!")

    slug = _slug(task)

    assert slug == "add-healthz-metrics"
    assert "/" not in slug and " " not in slug


def test_failed_run_escalates_to_a_human(repo: Path) -> None:
    queue = MemoryQueue()
    task = make_task(queue)
    transport = MemoryTransport()
    executor = make_executor(queue, repo, RecordingForge(), FakeRunner(ok=False), transport)

    evidence = executor.execute(task, make_spec(), recipient="dev@example.com")

    assert evidence.failure is FailureKind.HARNESS_FAILED
    assert "needs attention" in transport.outbox[0].subject


def test_no_changes_is_treated_as_a_failure_without_a_pr(repo: Path) -> None:
    queue = MemoryQueue()
    task = make_task(queue)
    transport = MemoryTransport()
    forge = RecordingForge()
    executor = make_executor(queue, repo, forge, FakeRunner(ok=True, write=False), transport)

    evidence = executor.execute(task, make_spec(), recipient="dev@example.com")

    assert evidence.failure is FailureKind.NO_CHANGES
    assert evidence.pr_url is None
    assert forge.calls == []
    assert queue.get("e1").state is TaskState.FAILED


def test_missing_harness_is_escalated_as_such(repo: Path) -> None:
    queue = MemoryQueue()
    task = make_task(queue)
    transport = MemoryTransport()
    executor = make_executor(queue, repo, RecordingForge(), MissingRunner(), transport)

    evidence = executor.execute(task, make_spec(), recipient="dev@example.com")

    assert evidence.failure is FailureKind.HARNESS_MISSING


class MissingRunner:
    def run(self, task: Task, spec: TaskSpec, worktree: Worktree) -> RunResult:
        return RunResult(exit_code=127, stderr="opencode: not found", branch=worktree.branch)


def test_report_skips_an_empty_recipient(tmp_path) -> None:

    from herald.executor import TaskExecutor
    from herald.gitplane.plane import GitPlane
    from herald.queue.memory import MemoryQueue

    repo = tmp_path / "repo"
    repo.mkdir()

    class NoNotifier:
        def status(self, *a, **k):
            raise AssertionError("must not notify an empty recipient")

    executor = TaskExecutor(
        queue=MemoryQueue(),
        runner=object(),  # type: ignore[arg-type]
        git=GitPlane(forge=object()),  # type: ignore[arg-type]
        repo_path=str(repo),
        worktrees_root=str(tmp_path / "wt"),
        notifier=NoNotifier(),  # type: ignore[arg-type]
    )

    class Evidence:
        task_id = "e1"
        failure = None
        ok = True
        run = None
        branch = "b"
        commit = "c"
        pr_url = None

    executor._report(Evidence(), recipient="")  # type: ignore[arg-type]

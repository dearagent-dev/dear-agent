from __future__ import annotations

import io
import json
from datetime import timedelta

from herald.cli.main import main
from herald.queue.memory import MemoryQueue
from herald.queue.models import Task, TaskState


def run(argv: list[str], queue: MemoryQueue) -> tuple[int, str]:
    import contextlib
    from unittest import mock

    buffer = io.StringIO()
    with (
        mock.patch("herald.cli.main.build_queue", return_value=queue),
        contextlib.redirect_stdout(buffer),
    ):
        code = main(argv)
    return code, buffer.getvalue()


def seed(queue: MemoryQueue, task_id: str = "e1", **overrides: object) -> Task:
    values: dict[str, object] = {
        "id": task_id,
        "transport_id": f"<{task_id}@example.com>",
        "subject": "add a health endpoint",
    }
    values.update(overrides)
    task = Task(**values)  # type: ignore[arg-type]
    stored = queue.enqueue(task)
    assert stored is not None
    return stored


def test_ls_lists_queued_tasks() -> None:
    queue = MemoryQueue()
    seed(queue, "e1")
    seed(queue, "e2")

    code, output = run(["task", "ls"], queue)

    assert code == 0
    assert "e1" in output and "e2" in output
    assert "queued" in output


def test_ls_is_empty_by_default() -> None:
    code, output = run(["task", "ls"], MemoryQueue())

    assert code == 0
    assert "no tasks" in output


def test_ls_json_emits_an_array() -> None:
    queue = MemoryQueue()
    seed(queue, "e1")

    code, output = run(["--json", "task", "ls"], queue)

    assert code == 0
    payload = json.loads(output)
    assert payload[0]["id"] == "e1"
    assert payload[0]["state"] == "queued"


def test_show_prints_a_missing_task_error() -> None:
    code, output = run(["task", "show", "missing"], MemoryQueue())

    assert code == 1
    assert "missing" in output


def test_claim_moves_to_running() -> None:
    queue = MemoryQueue()
    seed(queue, "e1")

    code, output = run(["task", "claim", "e1", "--lease-seconds", "60"], queue)

    assert code == 0
    assert "claimed e1" in output
    assert queue.get("e1").state is TaskState.RUNNING


def test_claim_rejects_a_non_queued_task() -> None:
    queue = MemoryQueue()
    task = seed(queue, "e1")
    queue.claim(task, lease=timedelta(seconds=60))

    code, output = run(["task", "claim", "e1"], queue)

    assert code == 1
    assert "not claimable" in output


def test_complete_finishes_a_running_task() -> None:
    queue = MemoryQueue()
    task = seed(queue, "e1")
    queue.claim(task, lease=timedelta(seconds=60))

    code, output = run(["task", "complete", "e1"], queue)

    assert code == 0
    assert "e1 -> done" in output
    assert queue.get("e1").state is TaskState.DONE


def test_fail_marks_a_running_task_failed() -> None:
    queue = MemoryQueue()
    task = seed(queue, "e1")
    queue.claim(task, lease=timedelta(seconds=60))

    code, output = run(["task", "fail", "e1"], queue)

    assert code == 0
    assert "e1 -> failed" in output
    assert queue.get("e1").state is TaskState.FAILED


def test_postgres_queue_without_dsn_errors_cleanly() -> None:
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    env = {
        "PATH": "",
        "PYTHONPATH": str(repo_root / "src"),
        "HERALD_QUEUE": "postgres",
        "HERALD_DATABASE_URL": "",
    }
    result = subprocess.run(
        [sys.executable, "-m", "herald.cli.main", "task", "ls"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 2
    assert "HERALD_DATABASE_URL" in result.stderr


def test_run_executes_a_task_via_the_worker(monkeypatch) -> None:
    import contextlib
    import io
    from unittest import mock

    from herald.executor import ExecutedTask
    from herald.runners.worktree import RunResult

    queue = MemoryQueue()
    seed(queue, "e1")

    captured: dict[str, object] = {}

    class FakeWorker:
        def run(self, task_id: str) -> ExecutedTask:
            captured["task_id"] = task_id
            return ExecutedTask(
                task_id=task_id,
                branch="herald/e1",
                commit="deadbeef",
                pr_url="https://example.com/pr/1",
                run=RunResult(exit_code=0, branch="herald/e1"),
            )

    buffer = io.StringIO()
    with (
        mock.patch("herald.cli.main.build_queue", return_value=queue),
        mock.patch("herald.worker_factory.build_transport", return_value=object()),
        mock.patch("herald.worker_factory.build_worker", return_value=FakeWorker()),
        contextlib.redirect_stdout(buffer),
    ):
        code = main(["--json", "run", "e1", "--repo", "/repos/source"])

    assert code == 0
    assert captured["task_id"] == "e1"
    payload = json.loads(buffer.getvalue())
    assert payload["pr_url"] == "https://example.com/pr/1"


def test_run_without_a_task_id_picks_the_oldest_queued() -> None:
    import contextlib
    import io
    from unittest import mock

    from herald.executor import ExecutedTask
    from herald.runners.worktree import RunResult

    queue = MemoryQueue()
    seed(queue, "e1")
    seed(queue, "e2")

    captured: dict[str, object] = {}

    class FakeWorker:
        def run(self, task_id: str) -> ExecutedTask:
            captured["task_id"] = task_id
            return ExecutedTask(
                task_id=task_id,
                branch=f"herald/{task_id}",
                commit="deadbeef",
                pr_url=None,
                run=RunResult(exit_code=0, branch=f"herald/{task_id}"),
            )

        def run_next(self) -> ExecutedTask:
            # A real worker claims the oldest atomically; the fake mirrors that.
            return self.run(queue.list(TaskState.QUEUED, limit=1)[0].id)

    buffer = io.StringIO()
    with (
        mock.patch("herald.cli.main.build_queue", return_value=queue),
        mock.patch("herald.worker_factory.build_transport", return_value=object()),
        mock.patch("herald.worker_factory.build_worker", return_value=FakeWorker()),
        contextlib.redirect_stdout(buffer),
    ):
        code = main(["run", "--repo", "/repos/source"])

    assert code == 0
    assert captured["task_id"] == "e1"


def test_decide_routes_with_the_rule_decider(monkeypatch) -> None:
    import contextlib
    import io

    monkeypatch.setenv("HERALD_DECIDER", "rules")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(["--json", "decide", "audit the auth module for timing side-channels"])

    assert code == 0
    payload = json.loads(buffer.getvalue())
    assert payload["model"] == "hosted"
    assert payload["needs_human"] >= 0.5


def test_decide_can_be_disabled(monkeypatch) -> None:
    import contextlib
    import io

    monkeypatch.setenv("HERALD_DECIDER", "none")
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        code = main(["decide", "anything"])

    assert code == 1
    assert "no decider" in buffer.getvalue()


def test_enqueue_creates_a_queued_task_with_a_spec() -> None:
    queue = MemoryQueue()

    code, output = run(
        ["task", "enqueue", "fix the typo in the CLI help", "--repo", "git@github.com:o/r.git"],
        queue,
    )

    assert code == 0
    assert "enqueued" in output
    tasks = queue.list(TaskState.QUEUED)
    assert len(tasks) == 1
    assert tasks[0].spec is not None
    assert tasks[0].spec.repo_url == "git@github.com:o/r.git"
    assert tasks[0].spec.instructions == "fix the typo in the CLI help"


def test_approval_pending_lists_issued_tokens(tmp_path, monkeypatch) -> None:
    from herald.approvals import FileApprovalStore

    path = tmp_path / "approvals.json"
    FileApprovalStore(path).issue("e1", "land")
    monkeypatch.setenv("HERALD_APPROVALS_FILE", str(path))
    monkeypatch.delenv("HERALD_QUEUE", raising=False)

    code, output = run(["--json", "approval", "pending"], MemoryQueue())

    assert code == 0
    payload = json.loads(output)
    assert payload[0]["task_id"] == "e1"
    assert payload[0]["action"] == "land"


def test_approval_approve_redeems_the_token(tmp_path, monkeypatch) -> None:
    from herald.approvals import FileApprovalStore
    from herald.approvals_service import ApprovalService

    path = tmp_path / "approvals.json"
    monkeypatch.setenv("HERALD_APPROVALS_FILE", str(path))
    monkeypatch.delenv("HERALD_QUEUE", raising=False)
    queue = MemoryQueue()
    seed(queue, "e1")
    task = queue.get("e1")
    assert task is not None
    queue.claim(task, lease=timedelta(hours=1))
    running = queue.get("e1")
    assert running is not None
    queue.transition(running, TaskState.ACTION)
    token = ApprovalService(FileApprovalStore(path), queue).request("e1", "land")

    code, output = run(["approval", "approve", token], queue)

    assert code == 0
    assert "e1 -> approved" in output
    assert queue.get("e1").state is TaskState.APPROVED


def test_approval_redeeming_twice_fails_cleanly(tmp_path, monkeypatch) -> None:
    from herald.approvals import FileApprovalStore
    from herald.approvals_service import ApprovalService

    path = tmp_path / "approvals.json"
    monkeypatch.setenv("HERALD_APPROVALS_FILE", str(path))
    monkeypatch.delenv("HERALD_QUEUE", raising=False)
    queue = MemoryQueue()
    seed(queue, "e1")
    task = queue.get("e1")
    assert task is not None
    queue.claim(task, lease=timedelta(hours=1))
    running = queue.get("e1")
    assert running is not None
    queue.transition(running, TaskState.ACTION)
    token = ApprovalService(FileApprovalStore(path), queue).request("e1", "land")
    run(["approval", "reject", token], queue)

    code, output = run(["approval", "reject", token], queue)

    assert code == 1
    assert queue.get("e1").state is TaskState.REJECTED


def test_health_reports_queue_counts() -> None:
    queue = MemoryQueue()
    seed(queue, "e1")
    seed(queue, "e2")

    code, output = run(["--json", "health"], queue)

    assert code == 0
    payload = json.loads(output)
    assert payload["ok"] is True
    assert payload["counts"]["queued"] == 2
    assert payload["counts"]["running"] == 0


def test_requeue_returns_a_failed_task_to_queued() -> None:
    queue = MemoryQueue()
    seed(queue, "e1")
    task = queue.get("e1")
    assert task is not None
    queue.claim(task, lease=timedelta(seconds=60))
    running = queue.get("e1")
    assert running is not None
    queue.transition(running, TaskState.FAILED)

    code, output = run(["task", "requeue", "e1"], queue)

    assert code == 0
    assert "e1 -> queued" in output
    assert queue.get("e1").state is TaskState.QUEUED


def _todo_repo(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("# TODO: handle the empty case\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
    )
    return repo


def test_idle_proposes_a_task_from_a_todo(tmp_path) -> None:
    repo = _todo_repo(tmp_path)
    queue = MemoryQueue()

    code, output = run(["idle", "--repo", str(repo)], queue)

    assert code == 0
    assert "proposed 1" in output
    tasks = queue.list(TaskState.QUEUED)
    assert len(tasks) == 1
    assert tasks[0].spec is not None
    assert "app.py" in tasks[0].spec.instructions


def test_idle_does_not_re_propose_the_same_work(tmp_path) -> None:
    repo = _todo_repo(tmp_path)
    queue = MemoryQueue()
    run(["idle", "--repo", str(repo)], queue)
    # Free the queue so a second tick would propose again if the id were random.
    queue.claim(queue.list(TaskState.QUEUED)[0], lease=timedelta(hours=1))

    code, output = run(["idle", "--repo", str(repo)], queue)

    assert code == 0
    assert "proposed 0" in output
    assert len(queue.list(TaskState.RUNNING)) == 1


def test_enqueue_is_idempotent_on_transport_id() -> None:
    queue = MemoryQueue()
    argv = [
        "task",
        "enqueue",
        "fix it",
        "--repo",
        "o/r",
        "--transport-id",
        "<manual-1@herald.local>",
    ]

    first, _ = run(argv, queue)
    second, output = run(argv, queue)

    assert first == 0
    assert second == 1
    assert "already exists" in output
    assert len(queue.list(TaskState.QUEUED)) == 1

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


def test_jmap_backend_without_token_errors_cleanly(monkeypatch: object) -> None:
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    env = {
        "PATH": "",
        "PYTHONPATH": str(repo_root / "src"),
        "FASTMAIL_API_TOKEN": "",
    }
    result = subprocess.run(
        [sys.executable, "-m", "herald.cli.main", "task", "--backend", "jmap", "ls"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 2
    assert "FASTMAIL_API_TOKEN" in result.stderr

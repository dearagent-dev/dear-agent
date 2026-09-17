from __future__ import annotations

from datetime import UTC

from herald.queue.models import Task, TaskState


def test_task_defaults() -> None:
    task = Task(id="e1", transport_id="<msg-1@example.com>")

    assert task.base_branch == "main"
    assert task.state is TaskState.RECEIVED
    assert task.attempts == 0
    assert task.lease_until is None
    assert task.artifacts == {}
    assert task.thread_id is None
    assert task.repo_url is None
    assert task.branch is None


def test_task_timestamps_are_timezone_aware_utc() -> None:
    task = Task(id="e1", transport_id="<msg-1@example.com>")

    assert task.created_at.tzinfo is not None
    assert task.updated_at.tzinfo is not None
    assert task.created_at.utcoffset() == UTC.utcoffset(None)


def test_task_state_values_match_the_documented_lifecycle() -> None:
    assert {state.value for state in TaskState} == {
        "received",
        "queued",
        "running",
        "action",
        "approved",
        "rejected",
        "done",
        "failed",
    }

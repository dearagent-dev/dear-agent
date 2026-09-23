from __future__ import annotations

from datetime import UTC

from dear_agent.queue.models import Task, TaskSpec, TaskState


def test_task_defaults() -> None:
    task = Task(id="e1", transport_id="<msg-1@example.com>")

    assert task.state is TaskState.RECEIVED
    assert task.attempts == 0
    assert task.lease_until is None
    assert task.thread_id is None
    assert task.sender is None
    assert task.subject is None


def test_task_timestamps_are_timezone_aware_utc() -> None:
    task = Task(id="e1", transport_id="<msg-1@example.com>")

    assert task.created_at.tzinfo is not None
    assert task.created_at.utcoffset() == UTC.utcoffset(None)


def test_task_spec_defaults() -> None:
    spec = TaskSpec(repo_url="https://example.com/owner/repo")

    assert spec.base_branch == "main"
    assert spec.instructions == ""
    assert spec.model_request is None


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

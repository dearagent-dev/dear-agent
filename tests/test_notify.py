from __future__ import annotations

from datetime import timedelta

import pytest

from dear_agent.approvals import MemoryApprovalStore
from dear_agent.approvals_service import ApprovalService, parse_reply
from dear_agent.notify.notifier import Notifier, TaskLinks
from dear_agent.queue.memory import MemoryQueue
from dear_agent.queue.models import Task, TaskState
from dear_agent.transports.memory import MemoryTransport


def make_task(queue: MemoryQueue, state: TaskState = TaskState.RUNNING) -> Task:
    stored = queue.enqueue(
        Task(id="e1", transport_id="<m1@x>", thread_id="t1", subject="add healthz")
    )
    assert stored is not None
    if state is TaskState.QUEUED:
        return stored
    queue.claim(stored, lease=timedelta(hours=1))
    running = queue.get("e1")
    if state is TaskState.RUNNING:
        return running
    return queue.transition(running, state)


def test_status_message_is_threaded_and_carries_links() -> None:
    queue = MemoryQueue()
    task = make_task(queue)
    transport = MemoryTransport()
    notifier = Notifier(transport)

    notifier.status(
        task,
        recipient="dev@example.com",
        summary="draft PR opened",
        links=TaskLinks(
            branch="dear-agent/add-healthz",
            commit="abc123",
            pr_url="https://example.com/o/r/pull/7",
        ),
    )

    sent = transport.outbox[0]
    assert sent.thread_id == task.thread_id
    assert sent.headers["to"] == "dev@example.com"
    assert sent.headers["in-reply-to"] == "<m1@x>"
    assert "state: running" in sent.body
    assert "PR: https://example.com/o/r/pull/7" in sent.body


def test_status_message_never_contains_attachments() -> None:
    queue = MemoryQueue()
    task = make_task(queue)
    transport = MemoryTransport()

    Notifier(transport).status(task, recipient="dev@example.com")

    assert not hasattr(transport.outbox[0], "attachments")


def test_approval_request_includes_a_redeemable_token() -> None:
    queue = MemoryQueue()
    task = make_task(queue, TaskState.ACTION)
    transport = MemoryTransport()
    approvals = ApprovalService(MemoryApprovalStore(), queue)
    notifier = Notifier(transport, approvals)

    sent = notifier.request_approval(task, recipient="dev@example.com", action="land")

    reply = parse_reply(sent.body)
    assert reply is not None
    assert reply.decision == "approved"
    assert approvals._store.get(reply.token) is not None  # noqa: SLF001 - assert issued


def test_approval_reply_drives_the_task_state() -> None:
    queue = MemoryQueue()
    make_task(queue, TaskState.ACTION)
    approvals = ApprovalService(MemoryApprovalStore(), queue)

    token = approvals.request("e1", "land")
    approvals.apply(parse_reply(f"reject {token}"))

    assert queue.get("e1").state is TaskState.REJECTED


def test_approval_request_without_service_is_an_error() -> None:
    queue = MemoryQueue()
    task = make_task(queue, TaskState.ACTION)

    with pytest.raises(RuntimeError):
        Notifier(MemoryTransport()).request_approval(task, recipient="x", action="land")

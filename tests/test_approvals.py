from __future__ import annotations

from datetime import timedelta

import pytest

from herald.approvals import (
    ExpiredTokenError,
    FileApprovalStore,
    MemoryApprovalStore,
    TokenAlreadyUsedError,
    UnknownTokenError,
)
from herald.approvals_service import ApprovalService, parse_reply
from herald.queue.memory import MemoryQueue
from herald.queue.models import Task, TaskState


class FakeClock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture()
def clock():
    from datetime import UTC, datetime

    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


def test_issue_then_redeem_marks_used(clock) -> None:
    store = MemoryApprovalStore(clock=clock)
    approval = store.issue("e1", "land")
    assert approval.used is False

    redeemed = store.redeem(approval.token)

    assert redeemed.used is True


def test_single_use_token_cannot_be_redeemed_twice(clock) -> None:
    store = MemoryApprovalStore(clock=clock)
    approval = store.issue("e1", "land")
    store.redeem(approval.token)

    with pytest.raises(TokenAlreadyUsedError):
        store.redeem(approval.token)


def test_unknown_token_is_rejected(clock) -> None:
    store = MemoryApprovalStore(clock=clock)
    with pytest.raises(UnknownTokenError):
        store.redeem("nope")


def test_expired_token_is_rejected(clock) -> None:
    store = MemoryApprovalStore(clock=clock, ttl=timedelta(hours=1))
    approval = store.issue("e1", "land")
    clock.advance(timedelta(hours=2))

    with pytest.raises(ExpiredTokenError):
        store.redeem(approval.token)


def test_pending_excludes_used(clock) -> None:
    store = MemoryApprovalStore(clock=clock)
    first = store.issue("e1", "land")
    store.issue("e2", "land")
    store.redeem(first.token)

    assert [approval.task_id for approval in store.pending()] == ["e2"]


def test_file_store_round_trips(tmp_path, clock) -> None:
    path = tmp_path / "approvals.json"
    store = FileApprovalStore(path, clock=clock)
    approval = store.issue("e1", "land")

    reopened = FileApprovalStore(path, clock=clock)
    assert reopened.get(approval.token).task_id == "e1"

    reopened.redeem(approval.token)
    assert FileApprovalStore(path, clock=clock).get(approval.token).used is True


def test_parse_reply_recognizes_approve_and_reject() -> None:
    assert parse_reply("please approve abcdefghijklmnop now").decision == "approved"
    assert parse_reply("reject abcdefghijklmnop").decision == "rejected"


def test_parse_reply_ignores_unrelated_text() -> None:
    assert parse_reply("no decision here") is None
    assert parse_reply("approve short") is None


def test_service_moves_task_to_approved(clock) -> None:
    queue = MemoryQueue(clock=clock)
    task = queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    queue.claim(task, lease=timedelta(hours=1))
    running = queue.get("e1")
    queue.transition(running, TaskState.ACTION)

    store = MemoryApprovalStore(clock=clock)
    service = ApprovalService(store, queue)
    token = service.request("e1", "land")

    service.apply(parse_reply(f"approve {token}"))

    assert queue.get("e1").state is TaskState.APPROVED


def test_service_moves_task_to_rejected(clock) -> None:
    queue = MemoryQueue(clock=clock)
    task = queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    queue.claim(task, lease=timedelta(hours=1))
    running = queue.get("e1")
    queue.transition(running, TaskState.ACTION)

    store = MemoryApprovalStore(clock=clock)
    service = ApprovalService(store, queue)
    token = service.request("e1", "land")

    service.apply(parse_reply(f"reject {token}"))

    assert queue.get("e1").state is TaskState.REJECTED


def test_apply_rejects_a_task_that_is_not_awaiting_a_decision(clock) -> None:
    from herald.approvals import ApprovalNotApplicableError

    queue = MemoryQueue(clock=clock)
    task = queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    queue.claim(task, lease=timedelta(hours=1))
    running = queue.get("e1")
    queue.transition(running, TaskState.ACTION)
    store = MemoryApprovalStore(clock=clock)
    service = ApprovalService(store, queue)
    token = service.request("e1", "land")
    # The task finishes before the reply arrives.
    queue.transition(queue.get("e1"), TaskState.DONE)

    with pytest.raises(ApprovalNotApplicableError):
        service.apply(parse_reply(f"approve {token}"))


def test_run_gate_releases_the_task_to_queued(clock) -> None:
    queue = MemoryQueue(clock=clock)
    task = queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    queue.claim(task, lease=timedelta(hours=1))
    running = queue.get("e1")
    queue.transition(running, TaskState.ACTION)

    store = MemoryApprovalStore(clock=clock)
    service = ApprovalService(store, queue)
    token = service.request("e1", "run")

    service.apply(parse_reply(f"approve {token}"))

    assert queue.get("e1").state is TaskState.QUEUED


def test_build_approval_store_defaults_to_memory(monkeypatch) -> None:
    from herald.approvals import build_approval_store

    monkeypatch.delenv("HERALD_QUEUE", raising=False)
    monkeypatch.delenv("HERALD_APPROVALS_FILE", raising=False)

    assert isinstance(build_approval_store(), MemoryApprovalStore)


def test_build_approval_store_uses_a_file_when_configured(monkeypatch, tmp_path) -> None:
    from herald.approvals import build_approval_store

    monkeypatch.delenv("HERALD_QUEUE", raising=False)
    monkeypatch.setenv("HERALD_APPROVALS_FILE", str(tmp_path / "approvals.json"))

    assert isinstance(build_approval_store(), FileApprovalStore)


def test_build_approval_store_postgres_requires_a_dsn(monkeypatch) -> None:
    from herald.approvals import build_approval_store

    monkeypatch.setenv("HERALD_QUEUE", "postgres")
    monkeypatch.delenv("HERALD_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError):
        build_approval_store()

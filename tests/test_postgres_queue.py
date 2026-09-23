from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from herald.queue.models import Task, TaskState
from herald.queue.port import Queue, StateConflictError, TaskNotFoundError

DSN = os.environ.get("HERALD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="HERALD_TEST_DATABASE_URL is not set")

START = datetime(2026, 1, 1, tzinfo=UTC)
LEASE = timedelta(hours=1)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def make_task(**overrides: object) -> Task:
    values: dict[str, object] = {"id": "e1", "transport_id": "<msg-1@example.com>"}
    values.update(overrides)
    return Task(**values)  # type: ignore[arg-type]


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture()
def queue(clock: FakeClock):
    pytest.importorskip("psycopg")
    from herald.queue.postgres import PostgresQueue, apply_schema, connect

    conn = connect(DSN)
    apply_schema(conn)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE herald_task")
    conn.commit()
    yield PostgresQueue(conn, clock=clock)
    conn.close()


def test_postgres_queue_satisfies_the_port(queue) -> None:
    assert isinstance(queue, Queue)


def test_enqueue_puts_the_task_in_queued(queue) -> None:
    stored = queue.enqueue(make_task(state=TaskState.RECEIVED))

    assert stored is not None
    assert stored.state is TaskState.QUEUED
    assert stored.created_at == START
    assert queue.get("e1") is not None


def test_enqueue_is_idempotent_on_transport_id(queue) -> None:
    queue.enqueue(make_task())
    duplicate = queue.enqueue(make_task(id="e2", transport_id="<msg-1@example.com>"))

    assert duplicate is None
    assert queue.get("e2") is None


def test_enqueue_dedupes_on_id_even_with_a_new_transport(queue) -> None:
    queue.enqueue(make_task(id="e1", transport_id="<a@x>"))

    duplicate = queue.enqueue(make_task(id="e1", transport_id="<b@x>"))

    assert duplicate is None
    assert len(queue.list(TaskState.QUEUED)) == 1


def test_enqueue_returns_an_independent_copy(queue) -> None:
    stored = queue.enqueue(make_task())
    assert stored is not None

    stored.sender = "mutated@example.com"
    stored.thread_id = "mutated-thread"

    fetched = queue.get("e1")
    assert fetched is not None
    assert fetched.sender is None
    assert fetched.thread_id is None


def test_get_unknown_id_returns_none(queue) -> None:
    assert queue.get("missing") is None


def test_enqueue_persists_the_parsed_spec(queue) -> None:
    from herald.queue.models import TaskSpec

    spec = TaskSpec(
        repo_url="git@github.com:o/r.git",
        instructions="do it",
        model_request="x/y",
        verify="pytest -q",
    )
    stored = queue.enqueue(make_task(spec=spec))

    assert stored is not None
    assert stored.spec == spec
    fetched = queue.get("e1")
    assert fetched is not None
    assert fetched.spec == spec


def test_list_filters_by_state(queue) -> None:
    queue.enqueue(make_task(id="e1"))
    second = queue.enqueue(make_task(id="e2", transport_id="<msg-2@example.com>"))
    assert second is not None

    assert [task.id for task in queue.list(TaskState.QUEUED)] == ["e1", "e2"]
    assert queue.list(TaskState.RUNNING) == []


def test_list_orders_oldest_first_and_respects_limit(queue, clock: FakeClock) -> None:
    queue.enqueue(make_task(id="e1"))
    clock.advance(timedelta(minutes=1))
    queue.enqueue(make_task(id="e2", transport_id="<msg-2@example.com>"))
    clock.advance(timedelta(minutes=1))
    queue.enqueue(make_task(id="e3", transport_id="<msg-3@example.com>"))

    assert [task.id for task in queue.list(TaskState.QUEUED)] == ["e1", "e2", "e3"]
    assert [task.id for task in queue.list(TaskState.QUEUED, limit=2)] == ["e1", "e2"]


def test_claim_moves_queued_to_running_with_a_lease(queue, clock: FakeClock) -> None:
    task = queue.enqueue(make_task())
    assert task is not None

    assert queue.claim(task, lease=LEASE) is True

    claimed = queue.get(task.id)
    assert claimed is not None
    assert claimed.state is TaskState.RUNNING
    assert claimed.lease_until == clock.now + LEASE


def test_claim_returns_false_when_not_queued(queue) -> None:
    task = queue.enqueue(make_task())
    assert task is not None
    queue.claim(task, lease=LEASE)

    assert queue.claim(task, lease=LEASE) is False


def test_claim_returns_false_for_unknown_task(queue) -> None:
    assert queue.claim(make_task(id="missing"), lease=LEASE) is False


def test_claim_next_claims_the_oldest_queued(queue, clock: FakeClock) -> None:
    queue.enqueue(make_task(id="e1"))
    clock.advance(timedelta(minutes=1))
    queue.enqueue(make_task(id="e2", transport_id="<msg-2@example.com>"))

    claimed = queue.claim_next(lease=LEASE)

    assert claimed is not None
    assert claimed.id == "e1"
    assert queue.get("e1").state is TaskState.RUNNING
    assert queue.get("e2").state is TaskState.QUEUED


def test_claim_next_returns_none_when_empty(queue) -> None:
    assert queue.claim_next(lease=LEASE) is None


def test_transition_raises_on_state_conflict(queue) -> None:
    task = queue.enqueue(make_task())
    assert task is not None
    queue.claim(task, lease=LEASE)

    with pytest.raises(StateConflictError):
        queue.transition(task, TaskState.DONE)


def test_transition_raises_for_unknown_task(queue) -> None:
    with pytest.raises(TaskNotFoundError):
        queue.transition(make_task(id="missing"), TaskState.DONE)


def test_transition_clears_the_lease_when_leaving_running(queue) -> None:
    task = queue.enqueue(make_task())
    assert task is not None
    queue.claim(task, lease=LEASE)
    running = queue.get(task.id)
    assert running is not None

    done = queue.transition(running, TaskState.DONE)

    assert done.state is TaskState.DONE
    assert done.lease_until is None


def test_transition_records_delivery_evidence(queue) -> None:
    from herald.queue.models import Evidence

    task = queue.enqueue(make_task())
    assert task is not None
    queue.claim(task, lease=LEASE)
    running = queue.get(task.id)
    assert running is not None

    done = queue.transition(
        running,
        TaskState.DONE,
        evidence=Evidence(branch="herald/x", commit="abc", pr_url="https://example.com/pr/1"),
    )

    assert done.evidence is not None
    assert done.evidence.pr_url == "https://example.com/pr/1"
    fetched = queue.get(task.id)
    assert fetched is not None
    assert fetched.evidence.branch == "herald/x"


def test_release_stale_requeues_expired_running_and_bumps_attempts(queue, clock: FakeClock) -> None:
    task = queue.enqueue(make_task())
    assert task is not None
    queue.claim(task, lease=LEASE)

    clock.advance(LEASE + timedelta(seconds=1))
    released = queue.release_stale(now=clock.now)

    assert [released_task.id for released_task in released] == [task.id]
    requeued = queue.get(task.id)
    assert requeued is not None
    assert requeued.state is TaskState.QUEUED
    assert requeued.attempts == 1
    assert requeued.lease_until is None


def test_concurrent_claim_next_takes_different_tasks(queue, clock: FakeClock) -> None:
    # Two workers, two connections: SKIP LOCKED must hand them different tasks.
    pytest.importorskip("psycopg")
    from herald.db import connect
    from herald.queue.postgres import PostgresQueue

    queue.enqueue(make_task(id="e1"))
    queue.enqueue(make_task(id="e2", transport_id="<msg-2@example.com>"))
    first_conn = connect(DSN)
    second_conn = connect(DSN)
    try:
        first = PostgresQueue(first_conn, clock=clock).claim_next(lease=LEASE)
        second = PostgresQueue(second_conn, clock=clock).claim_next(lease=LEASE)
    finally:
        first_conn.close()
        second_conn.close()

    assert first is not None and second is not None
    assert {first.id, second.id} == {"e1", "e2"}


def test_release_stale_ignores_unexpired_and_non_running(queue, clock: FakeClock) -> None:
    running = queue.enqueue(make_task(id="e1"))
    queued = queue.enqueue(make_task(id="e2", transport_id="<msg-2@example.com>"))
    assert running is not None and queued is not None
    queue.claim(running, lease=LEASE)

    assert queue.release_stale(now=clock.now) == []

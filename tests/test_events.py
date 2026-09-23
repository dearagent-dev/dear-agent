from __future__ import annotations

import os
from datetime import timedelta

import pytest

from herald.events import ErrorBudget, EventLog, MemoryEventLog
from herald.queue.models import utcnow

DSN = os.environ.get("HERALD_TEST_DATABASE_URL")


def test_memory_event_log_satisfies_the_port() -> None:
    assert isinstance(MemoryEventLog(), EventLog)


def test_record_and_read_are_append_only_and_per_task() -> None:
    log = MemoryEventLog()
    log.record("e1", "task.claimed")
    log.record("e2", "task.claimed")
    log.record("e1", "task.done", pr_url="https://pr/1")

    events = log.read("e1")

    assert [event.kind for event in events] == ["task.claimed", "task.done"]
    assert events[1].data["pr_url"] == "https://pr/1"
    assert [event.kind for event in log.recent(limit=1)] == ["task.done"]


def test_count_filters_by_kind_and_window() -> None:
    log = MemoryEventLog()
    log.record("e1", "task.failed")
    now = utcnow()

    assert log.count("task.failed", since=now - timedelta(minutes=1)) == 1
    assert log.count("task.failed", since=now + timedelta(minutes=1)) == 0
    assert log.count("task.done", since=now - timedelta(minutes=1)) == 0


def test_error_budget_is_disabled_by_default() -> None:
    log = MemoryEventLog()
    log.record("e1", "task.failed")

    assert ErrorBudget().exhausted(log) is False


def test_error_budget_trips_after_too_many_failures() -> None:
    log = MemoryEventLog()
    budget = ErrorBudget(max_failures=2, window_seconds=3600)
    log.record("e1", "task.failed")
    assert budget.exhausted(log) is False

    log.record("e2", "task.failed")

    assert budget.exhausted(log) is True


def test_error_budget_from_env() -> None:
    budget = ErrorBudget.from_env(
        {"HERALD_ERROR_BUDGET_FAILURES": "5", "HERALD_ERROR_BUDGET_WINDOW": "60"}
    )

    assert budget.max_failures == 5
    assert budget.window_seconds == 60


@pytest.mark.skipif(not DSN, reason="HERALD_TEST_DATABASE_URL is not set")
def test_postgres_event_log_round_trip() -> None:
    pytest.importorskip("psycopg")
    from herald.db import connect, init_schema
    from herald.events import PostgresEventLog

    conn = connect(DSN)
    init_schema(conn)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE herald_event")
    conn.commit()
    log = PostgresEventLog(conn)
    try:
        log.record("e1", "task.claimed")
        log.record("e1", "task.failed", failure="harness_failed")

        events = log.read("e1")

        assert [event.kind for event in events] == ["task.claimed", "task.failed"]
        assert events[1].data["failure"] == "harness_failed"
        assert log.count("task.failed", since=utcnow() - timedelta(hours=1)) == 1
    finally:
        conn.close()

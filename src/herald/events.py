from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from herald.queue.models import utcnow

EVENT_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS herald_event (
        id      bigserial PRIMARY KEY,
        at      timestamptz NOT NULL,
        task_id text NOT NULL,
        kind    text NOT NULL,
        data    jsonb NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "CREATE INDEX IF NOT EXISTS herald_event_task_idx ON herald_event (task_id, id)",
    "CREATE INDEX IF NOT EXISTS herald_event_kind_idx ON herald_event (kind, at)",
)


def apply_schema(conn: Any) -> None:
    """Create the event table if it does not exist. Idempotent."""
    with conn.cursor() as cur:
        for statement in EVENT_SCHEMA_STATEMENTS:
            cur.execute(statement)
    conn.commit()


@dataclass(slots=True, frozen=True)
class TaskEvent:
    """One typed, append-only fact about a task's lifecycle."""

    task_id: str
    kind: str
    at: datetime
    data: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class EventLog(Protocol):
    """An append-only task event log (ADR-adjacent, from OvernightAgent's event stream).

    Events are durable facts — claimed, verified, failed, done — that state columns cannot
    express as a history. Recording is best effort: an event failure must never break a run.
    """

    def record(self, task_id: str, kind: str, **data: Any) -> None: ...
    def read(self, task_id: str) -> list[TaskEvent]: ...
    def recent(self, *, limit: int = 50) -> list[TaskEvent]: ...
    def count(self, kind: str, *, since: datetime) -> int: ...


class MemoryEventLog:
    """In-memory event log for tests and single-process runs."""

    def __init__(self) -> None:
        self._events: list[TaskEvent] = []

    def record(self, task_id: str, kind: str, **data: Any) -> None:
        self._events.append(TaskEvent(task_id=task_id, kind=kind, at=utcnow(), data=data))

    def read(self, task_id: str) -> list[TaskEvent]:
        return [event for event in self._events if event.task_id == task_id]

    def recent(self, *, limit: int = 50) -> list[TaskEvent]:
        return self._events[-limit:]

    def count(self, kind: str, *, since: datetime) -> int:
        return sum(1 for event in self._events if event.kind == kind and event.at >= since)


class PostgresEventLog:
    """Durable event log in PostgreSQL, shared across processes (ADR 0005)."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def record(self, task_id: str, kind: str, **data: Any) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO herald_event (at, task_id, kind, data) VALUES (%s, %s, %s, %s::jsonb)",
                (utcnow(), task_id, kind, json.dumps(data, default=str)),
            )
        self._conn.commit()

    def read(self, task_id: str) -> list[TaskEvent]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT at, task_id, kind, data FROM herald_event WHERE task_id = %s ORDER BY id",
                (task_id,),
            )
            rows = cur.fetchall()
        self._conn.commit()
        return [_event_from_row(row) for row in rows]

    def recent(self, *, limit: int = 50) -> list[TaskEvent]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT at, task_id, kind, data FROM herald_event ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
        self._conn.commit()
        return [_event_from_row(row) for row in reversed(rows)]

    def count(self, kind: str, *, since: datetime) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM herald_event WHERE kind = %s AND at >= %s", (kind, since)
            )
            row = cur.fetchone()
        self._conn.commit()
        return int(row[0]) if row else 0


def _event_from_row(row: tuple) -> TaskEvent:
    at, task_id, kind, data = row
    return TaskEvent(task_id=task_id, kind=kind, at=at, data=data or {})


def build_event_log() -> EventLog:
    """Build the durable event log when the queue is PostgreSQL, else an in-memory one."""
    if os.environ.get("HERALD_QUEUE", "memory").strip().lower() == "postgres":
        from herald.db import connect, init_schema

        dsn = os.environ.get("HERALD_DATABASE_URL")
        if not dsn:
            raise RuntimeError("HERALD_DATABASE_URL is required for the postgres queue")
        conn = connect(dsn)
        init_schema(conn)
        return PostgresEventLog(conn)
    return MemoryEventLog()


@dataclass(slots=True)
class ErrorBudget:
    """A durable circuit breaker over recent failures (from OvernightAgent's error budget).

    When more than ``max_failures`` tasks failed within ``window_seconds``, the budget is
    exhausted and no new run starts until the window clears. ``max_failures <= 0`` disables it.
    """

    max_failures: int = 0
    window_seconds: int = 3600

    @classmethod
    def from_env(cls, env: dict[str, str]) -> ErrorBudget:
        return cls(
            max_failures=int(env.get("HERALD_ERROR_BUDGET_FAILURES", "0") or "0"),
            window_seconds=int(env.get("HERALD_ERROR_BUDGET_WINDOW", "3600") or "3600"),
        )

    def exhausted(self, events: EventLog, *, now: datetime | None = None) -> bool:
        if self.max_failures <= 0:
            return False
        since = (now or utcnow()) - timedelta(seconds=self.window_seconds)
        return events.count("task.failed", since=since) >= self.max_failures


__all__ = [
    "EVENT_SCHEMA_STATEMENTS",
    "ErrorBudget",
    "EventLog",
    "MemoryEventLog",
    "PostgresEventLog",
    "TaskEvent",
    "apply_schema",
    "build_event_log",
]

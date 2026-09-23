from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from herald.queue.models import Task, TaskSpec, TaskState, utcnow
from herald.queue.port import StateConflictError, TaskNotFoundError

# The durable queue (ADR 0005): one table, state as a column. Kept as a list of statements so
# it can be applied with psycopg's extended protocol (which forbids multi-statement queries).
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS herald_task (
        id           text PRIMARY KEY,
        transport_id text NOT NULL UNIQUE,
        thread_id    text,
        sender       text,
        subject      text,
        state        text NOT NULL,
        attempts     integer NOT NULL DEFAULT 0,
        lease_until  timestamptz,
        created_at   timestamptz NOT NULL DEFAULT now(),
        updated_at   timestamptz NOT NULL DEFAULT now(),
        spec_repo_url      text,
        spec_base_branch   text,
        spec_instructions  text,
        spec_model_request text,
        CONSTRAINT herald_task_state_valid CHECK (
            state IN ('received', 'queued', 'running', 'action',
                      'done', 'failed', 'rejected', 'approved')
        )
    )
    """,
    "ALTER TABLE herald_task ADD COLUMN IF NOT EXISTS spec_repo_url text",
    "ALTER TABLE herald_task ADD COLUMN IF NOT EXISTS spec_base_branch text",
    "ALTER TABLE herald_task ADD COLUMN IF NOT EXISTS spec_instructions text",
    "ALTER TABLE herald_task ADD COLUMN IF NOT EXISTS spec_model_request text",
    """
    CREATE INDEX IF NOT EXISTS herald_task_state_created_idx
        ON herald_task (state, created_at)
    """,
)

_COLUMNS = (
    "id, transport_id, thread_id, sender, subject, state, attempts, lease_until, created_at, "
    "spec_repo_url, spec_base_branch, spec_instructions, spec_model_request"
)


def connect(dsn: str) -> Any:
    """Open a connection to the database at ``dsn``. The caller owns its lifecycle."""
    import psycopg

    return psycopg.connect(dsn)


def apply_schema(conn: Any) -> None:
    """Create the queue table if it does not exist. Idempotent; safe to call at startup."""
    with conn.cursor() as cur:
        for statement in SCHEMA_STATEMENTS:
            cur.execute(statement)
    conn.commit()


def open_queue(dsn: str, *, clock: Callable[[], datetime] = utcnow) -> PostgresQueue:
    """Connect, apply the full schema and return a ready :class:`PostgresQueue`."""
    from herald.db import init_schema

    conn = connect(dsn)
    init_schema(conn)
    return PostgresQueue(conn, clock=clock)


class PostgresQueue:
    """A :class:`~herald.queue.port.Queue` over PostgreSQL (ADR 0005).

    State lives in a column, dedupe is a unique index on ``transport_id`` and every
    transition is a guarded ``UPDATE`` (compare-and-swap), so concurrent workers cannot take
    the same task or apply a transition twice. The lease deadline and attempt count are real
    columns, not encoded metadata.
    """

    def __init__(self, conn: Any, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._conn = conn
        self._clock = clock

    def enqueue(self, task: Task) -> Task | None:
        now = self._clock()
        spec = task.spec
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO herald_task ({_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, 0, NULL, %s, %s, %s, %s, %s)
                ON CONFLICT (transport_id) DO NOTHING
                RETURNING {_COLUMNS}
                """,
                (
                    task.id,
                    task.transport_id,
                    task.thread_id,
                    task.sender,
                    task.subject,
                    TaskState.QUEUED.value,
                    now,
                    spec.repo_url if spec else None,
                    spec.base_branch if spec else None,
                    spec.instructions if spec else None,
                    spec.model_request if spec else None,
                ),
            )
            row = cur.fetchone()
        self._conn.commit()
        return _task_from_row(row) if row else None

    def get(self, task_id: str) -> Task | None:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT {_COLUMNS} FROM herald_task WHERE id = %s", (task_id,))
            row = cur.fetchone()
        self._conn.commit()
        return _task_from_row(row) if row else None

    def list(self, state: TaskState, *, limit: int = 10) -> list[Task]:
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_COLUMNS} FROM herald_task
                 WHERE state = %s
                 ORDER BY created_at, id
                 LIMIT %s
                """,
                (state.value, limit),
            )
            rows = cur.fetchall()
        self._conn.commit()
        return [_task_from_row(row) for row in rows]

    def claim(self, task: Task, *, lease: timedelta) -> bool:
        now = self._clock()
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE herald_task
                   SET state = %s, lease_until = %s, updated_at = %s
                 WHERE id = %s AND state = %s
                 RETURNING {_COLUMNS}
                """,
                (TaskState.RUNNING.value, now + lease, now, task.id, TaskState.QUEUED.value),
            )
            row = cur.fetchone()
        self._conn.commit()
        return row is not None

    def transition(self, task: Task, to_state: TaskState) -> Task:
        now = self._clock()
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE herald_task
                   SET state = %s,
                       lease_until = CASE WHEN %s = 'running' THEN lease_until ELSE NULL END,
                       updated_at = %s
                 WHERE id = %s AND state = %s
                 RETURNING {_COLUMNS}
                """,
                (to_state.value, to_state.value, now, task.id, task.state.value),
            )
            row = cur.fetchone()
            current = None
            if row is None:
                cur.execute("SELECT state FROM herald_task WHERE id = %s", (task.id,))
                current = cur.fetchone()
        self._conn.commit()
        if row is not None:
            return _task_from_row(row)
        if current is None:
            raise TaskNotFoundError(task.id)
        raise StateConflictError(task.id, task.state, TaskState(current[0]))

    def release_stale(self, *, now: datetime) -> list[Task]:
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE herald_task
                   SET state = %s, attempts = attempts + 1, lease_until = NULL, updated_at = %s
                 WHERE state = %s AND lease_until IS NOT NULL AND lease_until < %s
                 RETURNING {_COLUMNS}
                """,
                (TaskState.QUEUED.value, now, TaskState.RUNNING.value, now),
            )
            rows = cur.fetchall()
        self._conn.commit()
        tasks = [_task_from_row(row) for row in rows]
        tasks.sort(key=lambda task: (task.created_at, task.id))
        return tasks


def _task_from_row(row: Sequence[Any]) -> Task:
    (
        id_,
        transport_id,
        thread_id,
        sender,
        subject,
        state,
        attempts,
        lease_until,
        created_at,
        spec_repo_url,
        spec_base_branch,
        spec_instructions,
        spec_model_request,
    ) = row
    spec = None
    if spec_repo_url is not None:
        spec = TaskSpec(
            repo_url=spec_repo_url,
            base_branch=spec_base_branch or "main",
            instructions=spec_instructions or "",
            model_request=spec_model_request,
        )
    return Task(
        id=id_,
        transport_id=transport_id,
        thread_id=thread_id,
        sender=sender,
        subject=subject,
        state=TaskState(state),
        attempts=attempts,
        lease_until=lease_until,
        created_at=created_at,
        spec=spec,
    )


__all__ = ["PostgresQueue", "SCHEMA_STATEMENTS", "apply_schema", "connect", "open_queue"]

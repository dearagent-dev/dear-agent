from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from dear_agent.queue.models import Evidence, Task, TaskSpec, TaskState, utcnow
from dear_agent.queue.port import StateConflictError, TaskNotFoundError

# The durable queue (ADR 0005): one table, state as a column. Kept as a list of statements so
# it can be applied with psycopg's extended protocol (which forbids multi-statement queries).
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS dear_agent_task (
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
        spec_verify        text,
        spec_depends_on    text,
        branch             text,
        commit             text,
        pr_url             text,
        CONSTRAINT dear_agent_task_state_valid CHECK (
            state IN ('received', 'queued', 'running', 'action',
                      'done', 'failed', 'rejected', 'approved')
        )
    )
    """,
    "ALTER TABLE dear_agent_task ADD COLUMN IF NOT EXISTS spec_repo_url text",
    "ALTER TABLE dear_agent_task ADD COLUMN IF NOT EXISTS spec_base_branch text",
    "ALTER TABLE dear_agent_task ADD COLUMN IF NOT EXISTS spec_instructions text",
    "ALTER TABLE dear_agent_task ADD COLUMN IF NOT EXISTS spec_model_request text",
    "ALTER TABLE dear_agent_task ADD COLUMN IF NOT EXISTS spec_verify text",
    "ALTER TABLE dear_agent_task ADD COLUMN IF NOT EXISTS spec_depends_on text",
    "ALTER TABLE dear_agent_task ADD COLUMN IF NOT EXISTS branch text",
    "ALTER TABLE dear_agent_task ADD COLUMN IF NOT EXISTS commit text",
    "ALTER TABLE dear_agent_task ADD COLUMN IF NOT EXISTS pr_url text",
    """
    CREATE INDEX IF NOT EXISTS dear_agent_task_state_created_idx
        ON dear_agent_task (state, created_at)
    """,
)

_COLUMNS = (
    "id, transport_id, thread_id, sender, subject, state, attempts, lease_until, created_at, "
    "spec_repo_url, spec_base_branch, spec_instructions, spec_model_request, spec_verify, "
    "spec_depends_on, branch, commit, pr_url"
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
    from dear_agent.db import init_schema

    conn = connect(dsn)
    init_schema(conn)
    return PostgresQueue(conn, clock=clock)


class PostgresQueue:
    """A :class:`~dear_agent.queue.port.Queue` over PostgreSQL (ADR 0005).

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
                INSERT INTO dear_agent_task ({_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, 0, NULL, %s, %s, %s, %s, %s, %s, %s,
                        NULL, NULL, NULL)
                ON CONFLICT DO NOTHING
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
                    spec.verify if spec else None,
                    json.dumps(list(spec.depends_on)) if spec else None,
                ),
            )
            row = cur.fetchone()
        self._conn.commit()
        return _task_from_row(row) if row else None

    def get(self, task_id: str) -> Task | None:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT {_COLUMNS} FROM dear_agent_task WHERE id = %s", (task_id,))
            row = cur.fetchone()
        self._conn.commit()
        return _task_from_row(row) if row else None

    def list(self, state: TaskState, *, limit: int = 10) -> list[Task]:
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_COLUMNS} FROM dear_agent_task
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
                UPDATE dear_agent_task
                   SET state = %s, lease_until = %s, updated_at = %s
                 WHERE id = %s AND state = %s
                 RETURNING {_COLUMNS}
                """,
                (TaskState.RUNNING.value, now + lease, now, task.id, TaskState.QUEUED.value),
            )
            row = cur.fetchone()
        self._conn.commit()
        return row is not None

    def claim_next(self, *, lease: timedelta) -> Task | None:
        now = self._clock()
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE dear_agent_task
                   SET state = %s, lease_until = %s, updated_at = %s
                 WHERE id = (
                     SELECT id FROM dear_agent_task
                      WHERE state = %s
                      ORDER BY created_at, id
                      FOR UPDATE SKIP LOCKED
                      LIMIT 1
                 )
                 RETURNING {_COLUMNS}
                """,
                (TaskState.RUNNING.value, now + lease, now, TaskState.QUEUED.value),
            )
            row = cur.fetchone()
        self._conn.commit()
        return _task_from_row(row) if row else None

    def transition(
        self,
        task: Task,
        to_state: TaskState,
        *,
        evidence: Evidence | None = None,
    ) -> Task:
        now = self._clock()
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE dear_agent_task
                   SET state = %s,
                       lease_until = CASE WHEN %s = 'running' THEN lease_until ELSE NULL END,
                       updated_at = %s,
                       branch = COALESCE(%s, branch),
                       commit = COALESCE(%s, commit),
                       pr_url = COALESCE(%s, pr_url)
                 WHERE id = %s AND state = %s
                 RETURNING {_COLUMNS}
                """,
                (
                    to_state.value,
                    to_state.value,
                    now,
                    evidence.branch if evidence else None,
                    evidence.commit if evidence else None,
                    evidence.pr_url if evidence else None,
                    task.id,
                    task.state.value,
                ),
            )
            row = cur.fetchone()
            current = None
            if row is None:
                cur.execute("SELECT state FROM dear_agent_task WHERE id = %s", (task.id,))
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
                UPDATE dear_agent_task
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
        spec_verify,
        spec_depends_on,
        branch,
        commit,
        pr_url,
    ) = row
    spec = None
    if spec_repo_url is not None:
        spec = TaskSpec(
            repo_url=spec_repo_url,
            base_branch=spec_base_branch or "main",
            instructions=spec_instructions or "",
            model_request=spec_model_request,
            verify=spec_verify,
            depends_on=tuple(json.loads(spec_depends_on)) if spec_depends_on else (),
        )
    evidence = None
    if branch is not None or commit is not None or pr_url is not None:
        evidence = Evidence(branch=branch, commit=commit, pr_url=pr_url)
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
        evidence=evidence,
    )


__all__ = ["PostgresQueue", "SCHEMA_STATEMENTS", "apply_schema", "connect", "open_queue"]

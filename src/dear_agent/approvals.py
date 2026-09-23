from __future__ import annotations

import hmac
import json
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

TOKEN_BYTES = 16
DEFAULT_TTL = timedelta(days=1)

APPROVAL_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS dear_agent_approval (
        token      text PRIMARY KEY,
        task_id    text NOT NULL,
        action     text NOT NULL,
        created_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,
        used_at    timestamptz
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS dear_agent_approval_task_idx
        ON dear_agent_approval (task_id)
    """,
)

_APPROVAL_COLUMNS = "token, task_id, action, created_at, expires_at, used_at"


def utcnow() -> datetime:
    return datetime.now(UTC)


class ApprovalError(Exception):
    """Base class for approval errors."""


class UnknownTokenError(ApprovalError):
    """The reply referenced a token that was never issued."""


class ExpiredTokenError(ApprovalError):
    """The token exists but is past its expiry."""


class TokenAlreadyUsedError(ApprovalError):
    """The token was already consumed; single-use is enforced."""


class ApprovalNotApplicableError(ApprovalError):
    """The token is valid but its task is not awaiting a decision."""

    def __init__(self, task_id: str, state: object) -> None:
        super().__init__(f"task {task_id!r} is {state} and does not need a decision")
        self.task_id = task_id
        self.state = state


@dataclass(slots=True)
class Approval:
    """A pending human decision for one task."""

    token: str
    task_id: str
    action: str
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None

    @property
    def used(self) -> bool:
        return self.used_at is not None

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


@runtime_checkable
class ApprovalStore(Protocol):
    """Store for issued approval tokens.

    A token cannot live on the immutable email, so it is kept beside the queue. The
    interface hides whether that is memory or a file.
    """

    def issue(self, task_id: str, action: str) -> Approval: ...
    def get(self, token: str) -> Approval | None: ...
    def redeem(self, token: str) -> Approval: ...
    def pending(self) -> list[Approval]: ...


class _ApprovalRegistry:
    """Shared issue/redeem logic; subclasses decide how to persist."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = utcnow,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._by_token: dict[str, Approval] = {}

    def issue(self, task_id: str, action: str) -> Approval:
        now = self._clock()
        approval = Approval(
            token=secrets.token_hex(TOKEN_BYTES),
            task_id=task_id,
            action=action,
            created_at=now,
            expires_at=now + self._ttl,
        )
        self._by_token[approval.token] = approval
        self._persist()
        return approval

    def get(self, token: str) -> Approval | None:
        return self._by_token.get(token)

    def redeem(self, token: str) -> Approval:
        approval = self._by_token.get(token)
        if approval is None:
            raise UnknownTokenError(token)
        if approval.used:
            raise TokenAlreadyUsedError(token)
        if approval.expired(self._clock()):
            raise ExpiredTokenError(token)
        approval.used_at = self._clock()
        self._persist()
        return approval

    def pending(self) -> list[Approval]:
        return [approval for approval in self._by_token.values() if not approval.used]

    def _persist(self) -> None:
        """Hook for durable subclasses; memory does nothing."""


class MemoryApprovalStore(_ApprovalRegistry):
    """In-memory approval store for tests and single-process runs."""


class FileApprovalStore(_ApprovalRegistry):
    """JSON-file approval store for a long-lived process; keep the path out of git."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] = utcnow,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        self._path = Path(path)
        super().__init__(clock=clock, ttl=ttl)
        if self._path.exists():
            self._load()

    def _persist(self) -> None:
        payload = {
            token: {
                "task_id": approval.task_id,
                "action": approval.action,
                "created_at": approval.created_at.isoformat(),
                "expires_at": approval.expires_at.isoformat(),
                "used_at": approval.used_at.isoformat() if approval.used_at else None,
            }
            for token, approval in self._by_token.items()
        }
        self._path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    def _load(self) -> None:
        raw = json.loads(self._path.read_text())
        for token, entry in raw.items():
            self._by_token[token] = Approval(
                token=token,
                task_id=entry["task_id"],
                action=entry["action"],
                created_at=datetime.fromisoformat(entry["created_at"]),
                expires_at=datetime.fromisoformat(entry["expires_at"]),
                used_at=datetime.fromisoformat(entry["used_at"]) if entry["used_at"] else None,
            )


def apply_schema(conn: Any) -> None:
    """Create the approval table if it does not exist. Idempotent."""
    with conn.cursor() as cur:
        for statement in APPROVAL_SCHEMA_STATEMENTS:
            cur.execute(statement)
    conn.commit()


class PostgresApprovalStore:
    """Approval tokens in PostgreSQL (ADR 0005).

    A token cannot live on the immutable email, and it must be shared across processes
    (the control plane issues and redeems; a runner Job may request). Single-use is enforced
    atomically by a conditional ``UPDATE ... WHERE used_at IS NULL``, so two concurrent
    redeems cannot both succeed.
    """

    def __init__(
        self,
        conn: Any,
        *,
        clock: Callable[[], datetime] = utcnow,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._ttl = ttl

    def issue(self, task_id: str, action: str) -> Approval:
        now = self._clock()
        approval = Approval(
            token=secrets.token_hex(TOKEN_BYTES),
            task_id=task_id,
            action=action,
            created_at=now,
            expires_at=now + self._ttl,
        )
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dear_agent_approval (token, task_id, action, created_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    approval.token,
                    approval.task_id,
                    approval.action,
                    approval.created_at,
                    approval.expires_at,
                ),
            )
        self._conn.commit()
        return approval

    def get(self, token: str) -> Approval | None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {_APPROVAL_COLUMNS} FROM dear_agent_approval WHERE token = %s", (token,)
            )
            row = cur.fetchone()
        self._conn.commit()
        return _approval_from_row(row) if row else None

    def redeem(self, token: str) -> Approval:
        now = self._clock()
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE dear_agent_approval
                   SET used_at = %s
                 WHERE token = %s AND used_at IS NULL AND expires_at > %s
                 RETURNING {_APPROVAL_COLUMNS}
                """,
                (now, token, now),
            )
            row = cur.fetchone()
            current = None
            if row is None:
                cur.execute(
                    f"SELECT {_APPROVAL_COLUMNS} FROM dear_agent_approval WHERE token = %s",
                    (token,),
                )
                current = cur.fetchone()
        self._conn.commit()
        if row is not None:
            return _approval_from_row(row)
        if current is None:
            raise UnknownTokenError(token)
        approval = _approval_from_row(current)
        if approval.used:
            raise TokenAlreadyUsedError(token)
        raise ExpiredTokenError(token)

    def pending(self) -> list[Approval]:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {_APPROVAL_COLUMNS} FROM dear_agent_approval "
                "WHERE used_at IS NULL ORDER BY created_at"
            )
            rows = cur.fetchall()
        self._conn.commit()
        return [_approval_from_row(row) for row in rows]


def _approval_from_row(row: tuple) -> Approval:
    token, task_id, action, created_at, expires_at, used_at = row
    return Approval(
        token=token,
        task_id=task_id,
        action=action,
        created_at=created_at,
        expires_at=expires_at,
        used_at=used_at,
    )


def build_approval_store() -> ApprovalStore:
    """Build the approval store from the environment (ADR 0005).

    With ``DEAR_AGENT_QUEUE=postgres`` the tokens live in the same database as the queue, so they
    are shared across processes; otherwise a file store is used when ``DEAR_AGENT_APPROVALS_FILE``
    is set, and memory as the last resort.
    """
    backend = os.environ.get("DEAR_AGENT_QUEUE", "memory").strip().lower()
    if backend == "postgres":
        from dear_agent.db import connect, init_schema

        dsn = os.environ.get("DEAR_AGENT_DATABASE_URL")
        if not dsn:
            raise RuntimeError("DEAR_AGENT_DATABASE_URL is required for the postgres queue")
        conn = connect(dsn)
        init_schema(conn)
        return PostgresApprovalStore(conn)
    path = os.environ.get("DEAR_AGENT_APPROVALS_FILE")
    if path:
        return FileApprovalStore(path)
    return MemoryApprovalStore()


def tokens_equal(left: str, right: str) -> bool:
    """Constant-time comparison, used when a token travels over an untrusted channel."""
    return hmac.compare_digest(left, right)


__all__ = [
    "APPROVAL_SCHEMA_STATEMENTS",
    "Approval",
    "ApprovalError",
    "ApprovalNotApplicableError",
    "ApprovalStore",
    "ExpiredTokenError",
    "FileApprovalStore",
    "MemoryApprovalStore",
    "PostgresApprovalStore",
    "TokenAlreadyUsedError",
    "UnknownTokenError",
    "apply_schema",
    "build_approval_store",
    "tokens_equal",
]

from __future__ import annotations

import contextlib
from typing import Any

import psycopg

# TCP keepalives so a long-lived control plane notices a dead peer instead of hanging.
_KEEPALIVES: dict[str, int] = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
}


def _open(dsn: str) -> Any:
    return psycopg.connect(dsn, **_KEEPALIVES)


class ResilientConnection:
    """A psycopg connection that reconnects on failure.

    The control plane is long-lived, so a dropped connection (server restart, idle timeout)
    must not break it permanently. This delegates ``cursor``/``commit``/``rollback`` and
    reconnects: lazily when the connection is closed, and once when a statement raises an
    operational error. Transactions here are short (mostly a single statement), so retrying
    the failed statement on a fresh connection is safe; a failure at ``commit`` propagates so
    the caller — or the queue's lease/attempts — can retry the whole operation.
    """

    def __init__(self, dsn: str, *, opener: Any = _open) -> None:
        self._dsn = dsn
        self._opener = opener
        self._conn: Any = None

    def _live(self) -> Any:
        if self._conn is None or self._conn.closed:
            self._conn = self._opener(self._dsn)
        return self._conn

    def _reconnect(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
        self._conn = None

    def cursor(self, *args: Any, **kwargs: Any) -> _ResilientCursor:
        return _ResilientCursor(self, self._live().cursor(*args, **kwargs))

    def commit(self) -> None:
        self._live().commit()

    def rollback(self) -> None:
        self._live().rollback()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def closed(self) -> bool:
        return self._conn is None or self._conn.closed

    def __enter__(self) -> ResilientConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._conn is not None:
            self._conn.__exit__(*exc)


class _ResilientCursor:
    """A cursor that re-runs a failed statement once on a fresh connection."""

    def __init__(self, connection: ResilientConnection, cursor: Any) -> None:
        self._connection = connection
        self._cursor = cursor

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._cursor.execute(*args, **kwargs)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            self._connection._reconnect()
            self._cursor = self._connection._live().cursor()
            return self._cursor.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __enter__(self) -> _ResilientCursor:
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc: object) -> Any:
        return self._cursor.__exit__(*exc)


def connect(dsn: str) -> ResilientConnection:
    """Open a resilient connection to the database at ``dsn``. The caller owns its lifecycle."""
    return ResilientConnection(dsn)


def init_schema(conn: Any) -> None:
    """Create every table Dear Agent needs. Idempotent; safe to call at startup.

    Each store owns its statements; this is just the composition root so a single
    ``init_schema`` call leaves the database ready for the queue and the approval store.
    """
    from dear_agent.approvals import apply_schema as apply_approvals
    from dear_agent.decision.log import apply_schema as apply_decisions
    from dear_agent.events import apply_schema as apply_events
    from dear_agent.policy import apply_schema as apply_policies
    from dear_agent.queue.postgres import apply_schema as apply_queue

    apply_queue(conn)
    apply_approvals(conn)
    apply_decisions(conn)
    apply_events(conn)
    apply_policies(conn)


__all__ = ["ResilientConnection", "connect", "init_schema"]

from __future__ import annotations

from typing import Any


def connect(dsn: str) -> Any:
    """Open a connection to the database at ``dsn``. The caller owns its lifecycle."""
    import psycopg

    return psycopg.connect(dsn)


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


__all__ = ["connect", "init_schema"]

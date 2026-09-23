from __future__ import annotations

from typing import Any


def connect(dsn: str) -> Any:
    """Open a connection to the database at ``dsn``. The caller owns its lifecycle."""
    import psycopg

    return psycopg.connect(dsn)


def init_schema(conn: Any) -> None:
    """Create every table Herald needs. Idempotent; safe to call at startup.

    Each store owns its statements; this is just the composition root so a single
    ``init_schema`` call leaves the database ready for the queue and the approval store.
    """
    from herald.approvals import apply_schema as apply_approvals
    from herald.decision.log import apply_schema as apply_decisions
    from herald.events import apply_schema as apply_events
    from herald.policy import apply_schema as apply_policies
    from herald.queue.postgres import apply_schema as apply_queue

    apply_queue(conn)
    apply_approvals(conn)
    apply_decisions(conn)
    apply_events(conn)
    apply_policies(conn)


__all__ = ["connect", "init_schema"]

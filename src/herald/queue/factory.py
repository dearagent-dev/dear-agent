from __future__ import annotations

import os

from herald.queue.memory import MemoryQueue
from herald.queue.port import Queue


def build_queue() -> Queue:
    """Build the queue named by ``HERALD_QUEUE`` (default: memory).

    The durable backend is ``postgres`` and requires ``HERALD_DATABASE_URL`` (ADR 0005); the
    mailbox is ingress, not a queue. ``memory`` keeps the CLI and tests offline.
    """
    backend = os.environ.get("HERALD_QUEUE", "memory").strip().lower()
    if backend == "memory":
        return MemoryQueue()
    if backend == "postgres":
        dsn = os.environ.get("HERALD_DATABASE_URL")
        if not dsn:
            raise RuntimeError("HERALD_DATABASE_URL is required for the postgres queue")
        from herald.queue.postgres import open_queue

        return open_queue(dsn)
    raise RuntimeError(f"unknown queue backend {backend!r}")


__all__ = ["build_queue"]

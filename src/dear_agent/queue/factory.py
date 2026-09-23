from __future__ import annotations

import os

from dear_agent.queue.memory import MemoryQueue
from dear_agent.queue.port import Queue


def build_queue() -> Queue:
    """Build the queue named by ``DEAR_AGENT_QUEUE`` (default: memory).

    The durable backend is ``postgres`` and requires ``DEAR_AGENT_DATABASE_URL`` (ADR 0005); the
    mailbox is ingress, not a queue. ``memory`` keeps the CLI and tests offline.
    """
    backend = os.environ.get("DEAR_AGENT_QUEUE", "memory").strip().lower()
    if backend == "memory":
        return MemoryQueue()
    if backend == "postgres":
        dsn = os.environ.get("DEAR_AGENT_DATABASE_URL")
        if not dsn:
            raise RuntimeError("DEAR_AGENT_DATABASE_URL is required for the postgres queue")
        from dear_agent.queue.postgres import open_queue

        return open_queue(dsn)
    raise RuntimeError(f"unknown queue backend {backend!r}")


__all__ = ["build_queue"]

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, TextIO

from herald.queue.memory import MemoryQueue
from herald.queue.port import Queue


@dataclass(slots=True)
class CliContext:
    queue: Queue | None
    as_json: bool
    out: TextIO

    def require_queue(self) -> Queue:
        if self.queue is None:
            raise RuntimeError("this command requires a queue backend")
        return self.queue

    def emit(self, message: str) -> None:
        print(message, file=self.out)

    def emit_json(self, payload: Any) -> None:
        import json

        print(json.dumps(payload, indent=2, default=str), file=self.out)


def build_queue(args: Any) -> Queue:
    """Build the queue backend named by ``--backend``.

    Defaults to the in-memory backend so the CLI runs without network. The JMAP backend
    requires ``FASTMAIL_API_TOKEN`` and optionally ``FASTMAIL_SESSION_URL``.
    """
    backend = getattr(args, "backend", "memory")
    if backend == "memory":
        return MemoryQueue()

    if backend == "jmap":
        from herald.jmap.client import DEFAULT_SESSION_URL, JmapClient
        from herald.queue.jmap import JmapQueue

        token = os.environ.get("FASTMAIL_API_TOKEN")
        if not token:
            raise RuntimeError("FASTMAIL_API_TOKEN is required for --backend jmap")

        client = JmapClient(
            token,
            account_id=os.environ.get("FASTMAIL_ACCOUNT_ID"),
            session_url=os.environ.get("FASTMAIL_SESSION_URL", DEFAULT_SESSION_URL),
        )
        client.connect()
        return JmapQueue(client, mailbox_name=os.environ.get("HERALD_MAILBOX", "Herald"))

    raise RuntimeError(f"unknown backend {backend!r}")

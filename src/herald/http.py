from __future__ import annotations

import os
import signal
import sys

from herald.http_server import HealthServer
from herald.queue.memory import MemoryQueue
from herald.queue.port import Queue


def build_queue() -> Queue:
    """Build the queue named by ``HERALD_BACKEND`` (default: memory)."""
    backend = os.environ.get("HERALD_BACKEND", "memory")
    if backend == "memory":
        return MemoryQueue()
    if backend == "jmap":
        from herald.jmap.client import DEFAULT_SESSION_URL, JmapClient
        from herald.queue.jmap import JmapQueue

        token = os.environ.get("FASTMAIL_API_TOKEN")
        if not token:
            raise RuntimeError("FASTMAIL_API_TOKEN is required for the jmap backend")
        client = JmapClient(
            token,
            account_id=os.environ.get("FASTMAIL_ACCOUNT_ID"),
            session_url=os.environ.get("FASTMAIL_SESSION_URL", DEFAULT_SESSION_URL),
        )
        client.connect()
        return JmapQueue(client, mailbox_name=os.environ.get("HERALD_MAILBOX", "Herald"))
    raise RuntimeError(f"unknown backend {backend!r}")


def main() -> int:
    queue = build_queue()
    server = HealthServer(
        queue=queue,
        host=os.environ.get("HERALD_HOST", "0.0.0.0"),
        port=int(os.environ.get("HERALD_PORT", "8080")),
        max_running=int(os.environ.get("HERALD_MAX_RUNNING", "1")),
    )

    def shutdown(*_: object) -> None:
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.start()
    signal.pause()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

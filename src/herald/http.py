from __future__ import annotations

import os
import signal
import sys

from herald.http_server import HealthServer
from herald.queue.memory import MemoryQueue
from herald.queue.port import Queue


def build_inbound(queue: Queue):
    """Build the inbound webhook handler from the environment, or ``None`` if no secret.

    The webhook is authenticated with ``HERALD_INBOUND_SECRET`` (HMAC). Without it the
    endpoint is disabled (503), so a misconfigured deploy fails closed rather than
    ingesting anonymous mail.
    """
    import os

    secret = os.environ.get("HERALD_INBOUND_SECRET")
    if not secret:
        return None

    from datetime import timedelta

    from herald.auth import InboundAuthorizer, InboundGate, RateLimiter
    from herald.control_plane import ControlPlane
    from herald.inbound import InboundWebhook
    from herald.worker_factory import build_transport

    transport = build_transport()
    gate = InboundGate(
        authorizer=InboundAuthorizer(secret=secret),
        rate_limiter=RateLimiter(
            limit=int(os.environ.get("HERALD_RATE_LIMIT", "60")),
            window=timedelta(minutes=1),
        ),
    )
    # Auth lives on the webhook (it maps failures to 401/403); the plane must not gate
    # again, or a rejected request would be silently swallowed as "denied".
    plane = ControlPlane(transport=transport, queue=queue)
    recipient = os.environ.get("HERALD_RECIPIENT")
    return InboundWebhook(control_plane=plane, gate=gate, recipient=recipient)


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
        max_body_bytes=int(os.environ.get("HERALD_MAX_BODY_BYTES", "1048576")),
        timeout_seconds=float(os.environ.get("HERALD_HTTP_TIMEOUT", "15")),
        inbound=build_inbound(queue),
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

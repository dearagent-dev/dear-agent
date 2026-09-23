from __future__ import annotations

import os
import signal
import sys

from dear_agent.http_server import HealthServer
from dear_agent.queue.factory import build_queue
from dear_agent.queue.port import Queue


def build_inbound(queue: Queue):
    """Build the inbound webhook handler from the environment, or ``None`` if no secret.

    The webhook is authenticated with ``DEAR_AGENT_INBOUND_SECRET`` (HMAC). Without it the
    endpoint is disabled (503), so a misconfigured deploy fails closed rather than
    ingesting anonymous mail.
    """
    import os

    secret = os.environ.get("DEAR_AGENT_INBOUND_SECRET")
    if not secret:
        return None

    from datetime import timedelta

    from dear_agent.approvals import build_approval_store
    from dear_agent.approvals_service import ApprovalService
    from dear_agent.auth import InboundAuthorizer, InboundGate, RateLimiter
    from dear_agent.control_plane import ControlPlane
    from dear_agent.decision.factory import build_decider
    from dear_agent.decision.router import HumanGate
    from dear_agent.decision.security import SecurityDecider
    from dear_agent.events import build_event_log
    from dear_agent.inbound import InboundWebhook
    from dear_agent.policy import build_policy_store
    from dear_agent.security import InjectionScanner
    from dear_agent.worker_factory import build_transport

    transport = build_transport()
    gate = InboundGate(
        authorizer=InboundAuthorizer(secret=secret),
        rate_limiter=RateLimiter(
            limit=int(os.environ.get("DEAR_AGENT_RATE_LIMIT", "60")),
            window=timedelta(minutes=1),
        ),
    )
    from dear_agent.notify.notifier import Notifier

    approvals = ApprovalService(build_approval_store(), queue)
    decider = build_decider()
    # Auth lives on the webhook (it maps failures to 401/403); the plane must not gate
    # again, or a rejected request would be silently swallowed as "denied".
    plane = ControlPlane(
        transport=transport,
        queue=queue,
        notifier=Notifier(transport),
        approvals=approvals,
        scanner=InjectionScanner(),
        security=SecurityDecider(decider=decider),
        human_gate=HumanGate(decider=decider),
        events=build_event_log(),
        policies=build_policy_store(),
    )
    recipient = os.environ.get("DEAR_AGENT_RECIPIENT")
    return InboundWebhook(control_plane=plane, gate=gate, recipient=recipient)


def main() -> int:
    queue = build_queue()
    server = HealthServer(
        queue=queue,
        host=os.environ.get("DEAR_AGENT_HOST", "0.0.0.0"),
        port=int(os.environ.get("DEAR_AGENT_PORT", "8080")),
        max_running=int(os.environ.get("DEAR_AGENT_MAX_RUNNING", "1")),
        max_body_bytes=int(os.environ.get("DEAR_AGENT_MAX_BODY_BYTES", "1048576")),
        timeout_seconds=float(os.environ.get("DEAR_AGENT_HTTP_TIMEOUT", "15")),
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

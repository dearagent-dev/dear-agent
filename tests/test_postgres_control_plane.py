from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from dear_agent.approvals import PostgresApprovalStore
from dear_agent.approvals_service import ApprovalService
from dear_agent.auth import InboundAuthorizer, InboundGate, RateLimiter, sign
from dear_agent.control_plane import ControlPlane
from dear_agent.notify.notifier import Notifier
from dear_agent.queue.models import TaskState
from dear_agent.transports.base import RawMessage
from dear_agent.transports.memory import MemoryTransport

DSN = os.environ.get("DEAR_AGENT_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="DEAR_AGENT_TEST_DATABASE_URL is not set")

BASE = datetime(2026, 1, 1, tzinfo=UTC)
SENDER = "dev@example.com"
SECRET = "s3cret"
BODY = "repo: https://github.com/owner/repo\n\nfix the build"


def make_message(body: str = BODY, **overrides: object) -> RawMessage:
    values: dict[str, object] = {
        "transport_id": "<m1@x>",
        "thread_id": "t1",
        "sender": SENDER,
        "subject": "[dear-agent] owner/repo: fix the build",
        "body": body,
        "headers": {"X-Dear-Agent-Signature": sign(body, SECRET, sender=SENDER)},
        "received_at": BASE,
    }
    values.update(overrides)
    return RawMessage(**values)  # type: ignore[arg-type]


def make_gate() -> InboundGate:
    return InboundGate(
        authorizer=InboundAuthorizer(secret=SECRET),
        rate_limiter=RateLimiter(limit=100, window=timedelta(hours=1)),
    )


@pytest.fixture()
def conn():
    pytest.importorskip("psycopg")
    from dear_agent.db import connect, init_schema

    connection = connect(DSN)
    init_schema(connection)
    with connection.cursor() as cur:
        cur.execute("TRUNCATE dear_agent_task, dear_agent_approval")
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def queue(conn):
    from dear_agent.queue.postgres import PostgresQueue

    return PostgresQueue(conn)


def test_ingest_persists_a_task_and_its_spec(queue) -> None:
    plane = ControlPlane(transport=MemoryTransport(), queue=queue, gate=make_gate())

    report = plane.ingest([make_message()])

    assert report.accepted == ["<m1@x>"]
    task = queue.get("<m1@x>")
    assert task is not None
    assert task.state is TaskState.QUEUED
    assert task.spec is not None
    assert task.spec.repo_url == "https://github.com/owner/repo"
    assert task.spec.instructions == "fix the build"


def test_redelivery_is_idempotent_on_the_database(queue) -> None:
    plane = ControlPlane(transport=MemoryTransport(), queue=queue, gate=make_gate())

    plane.ingest([make_message()])
    report = plane.ingest([make_message()])

    assert report.accepted == []
    assert len(queue.list(TaskState.QUEUED)) == 1


def test_approval_round_trip_on_the_database(queue, conn) -> None:
    transport = MemoryTransport()
    approvals = ApprovalService(PostgresApprovalStore(conn), queue)
    plane = ControlPlane(
        transport=transport,
        queue=queue,
        notifier=Notifier(transport),
        gate=make_gate(),
        approvals=approvals,
    )
    plane.ingest([make_message()])
    task = queue.get("<m1@x>")
    assert task is not None
    queue.claim(task, lease=timedelta(hours=1))
    running = queue.get("<m1@x>")
    assert running is not None
    queue.transition(running, TaskState.ACTION)
    token = approvals.request("<m1@x>", "land")
    body = f"approve {token}"

    report = plane.ingest(
        [
            make_message(
                body=body, headers={"X-Dear-Agent-Signature": sign(body, SECRET, sender=SENDER)}
            )
        ]
    )

    assert report.decided == ["<m1@x>"]
    decided = queue.get("<m1@x>")
    assert decided is not None
    assert decided.state is TaskState.APPROVED

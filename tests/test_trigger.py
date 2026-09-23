from __future__ import annotations

from dear_agent.control_plane import ControlPlane
from dear_agent.jmap.trigger import IngestOnChange, _touches_email
from dear_agent.queue.memory import MemoryQueue
from dear_agent.transports.base import RawMessage
from dear_agent.transports.memory import MemoryTransport


def test_touches_email_only_for_email_state_changes() -> None:
    assert _touches_email({"@type": "StateChange", "changed": {"u1": {"Email": "s"}}})
    assert not _touches_email({"@type": "StateChange", "changed": {"u1": {"Mailbox": "s"}}})
    assert not _touches_email({"@type": "ping"})


def test_email_change_triggers_a_poll_and_ingest() -> None:
    transport = MemoryTransport()
    transport.receive(
        RawMessage(
            transport_id="<m1@x>",
            sender="dev@example.com",
            subject="add healthz",
            body="repo: https://github.com/o/r\nadd a /healthz endpoint",
        )
    )
    queue = MemoryQueue()
    callback = IngestOnChange(
        control_plane=ControlPlane(transport=transport, queue=queue), transport=transport
    )

    callback({"@type": "StateChange", "changed": {"u1": {"Email": "s"}}})

    assert callback.last is not None
    assert callback.last.accepted == ["<m1@x>"]


def test_non_email_change_does_not_poll() -> None:
    transport = MemoryTransport()
    callback = IngestOnChange(
        control_plane=ControlPlane(transport=transport, queue=MemoryQueue()), transport=transport
    )

    callback({"@type": "StateChange", "changed": {"u1": {"Mailbox": "s"}}})

    assert callback.last is None

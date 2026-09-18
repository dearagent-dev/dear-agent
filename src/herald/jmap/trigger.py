from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from herald.control_plane import ControlPlane, IngestReport
from herald.jmap.eventsource import EventSourceListener
from herald.transports.port import Transport


@dataclass(slots=True)
class IngestOnChange:
    """A JMAP EventSource callback that re-polls on any Email state change.

    A ``StateChange`` says *that* mail changed, not what changed, so the correct reaction is
    to run the normal ingest pass (which is idempotent). Non-Email changes are ignored.
    """

    control_plane: ControlPlane
    transport: Transport
    recipient: str | None = None
    last: IngestReport | None = None

    def __call__(self, event: dict[str, Any]) -> bool | None:
        if not _touches_email(event):
            return None
        messages = self.transport.poll()
        self.last = self.control_plane.ingest(messages, recipient=self.recipient)
        return None


def _touches_email(event: dict[str, Any]) -> bool:
    if event.get("@type") != "StateChange":
        return False
    changed = event.get("changed", {})
    return any("Email" in types for types in changed.values())


@dataclass(slots=True)
class EventSourceLoop:
    """Binds a listener to an ingest callback for a single run."""

    listener: EventSourceListener
    callback: IngestOnChange

    def run(self) -> list[dict[str, Any]]:
        return self.listener.run()


__all__ = ["EventSourceLoop", "IngestOnChange"]

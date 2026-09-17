from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class TaskState(StrEnum):
    """Lifecycle states from ``docs/queue.md``.

    ``received`` and ``queued`` are transient; ``done``, ``failed``, ``approved`` and
    ``rejected`` are terminal.
    """

    RECEIVED = "received"
    QUEUED = "queued"
    RUNNING = "running"
    ACTION = "action"
    DONE = "done"
    FAILED = "failed"
    REJECTED = "rejected"
    APPROVED = "approved"


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


@dataclass(slots=True)
class Task:
    """A parsed inbound message.

    The authoritative copy is the email in the mailbox; Herald keeps no database. ``id``
    is the JMAP ``Email`` id assigned by the server and ``transport_id`` is the RFC 5322
    ``Message-ID`` (the dedupe key). ``artifacts`` are delivered as threaded replies and
    are not stored on the email.
    """

    id: str
    transport_id: str
    thread_id: str | None = None
    sender: str | None = None
    repo_url: str | None = None
    base_branch: str = "main"
    instructions: str = ""
    model_request: str | None = None
    branch: str | None = None
    state: TaskState = TaskState.RECEIVED
    attempts: int = 0
    lease_until: datetime | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

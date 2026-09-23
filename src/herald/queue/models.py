from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


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
    """Queue record for an inbound message, persisted as a row (ADR 0005).

    ``id`` is Herald's stable internal task id and ``transport_id`` is the RFC 5322
    ``Message-ID`` (the dedupe key, a unique index in the database). Content that lives in
    the message body is modelled by :class:`TaskSpec` and parsed by the Normalizer.
    """

    id: str
    transport_id: str
    thread_id: str | None = None
    sender: str | None = None
    subject: str | None = None
    state: TaskState = TaskState.RECEIVED
    attempts: int = 0
    lease_until: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class TaskSpec:
    """Task content parsed from the message body by the Normalizer.

    Never persisted in the mailbox: JMAP emails are immutable, so this is derived on demand
    from the message body.
    """

    repo_url: str
    base_branch: str = "main"
    instructions: str = ""
    model_request: str | None = None

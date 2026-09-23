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
class Evidence:
    """The delivered artifact for a task: its branch, commit and draft PR (ADR 0005).

    Persisted with the task so ``herald task show`` can point at the PR without reading the
    mailbox. Source never travels over the transport; this is a link, not content.
    """

    branch: str | None = None
    commit: str | None = None
    pr_url: str | None = None


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
    # Parsed content, persisted with the task so a runner never needs the mailbox to run
    # (ADR 0005). ``None`` for a task enqueued before the normalizer attached a spec.
    spec: TaskSpec | None = None
    # Delivered artifact links, recorded when the run finishes.
    evidence: Evidence | None = None


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
    # A command the change must pass before a PR is opened. It runs only if it is on the
    # operator's allowlist (golden rule 7: never execute a message-derived command blindly).
    verify: str | None = None
    # Task ids that must be `done` before this one runs (a typed dependency graph).
    depends_on: tuple[str, ...] = ()

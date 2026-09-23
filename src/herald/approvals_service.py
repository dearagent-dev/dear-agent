from __future__ import annotations

import re
from dataclasses import dataclass

from herald.approvals import (
    ApprovalStore,
    ExpiredTokenError,
    TokenAlreadyUsedError,
    UnknownTokenError,
)
from herald.queue.models import Task, TaskState
from herald.queue.port import Queue

TOKEN_PATTERN = re.compile(r"\b(?:approve|reject|herald)\s+([A-Za-z0-9_\-]{16,})\b", re.IGNORECASE)


@dataclass(slots=True, frozen=True)
class ApprovalReply:
    """A parsed inbound reply that decides a pending approval."""

    token: str
    decision: str  # "approved" or "rejected"


def parse_reply(text: str) -> ApprovalReply | None:
    """Extract ``approve <token>`` / ``reject <token>`` from a reply body.

    Returns ``None`` when no decision is present, so unrelated replies are ignored.
    """
    match = TOKEN_PATTERN.search(text)
    if match is None:
        return None
    word = match.group(0).split()[0].lower()
    decision = "approved" if word == "approve" else "rejected"
    return ApprovalReply(token=match.group(1), decision=decision)


class ApprovalService:
    """Redeems approval replies and drives the task through its state machine."""

    def __init__(self, store: ApprovalStore, queue: Queue) -> None:
        self._store = store
        self._queue = queue

    def request(self, task_id: str, action: str) -> str:
        """Issue a token and return it; the notifier sends it, never stores it on the email."""
        return self._store.issue(task_id, action).token

    def apply(self, reply: ApprovalReply) -> Task:
        """Redeem ``reply`` and move its task to the state the action implies.

        A ``run`` approval releases the task to ``queued``; a ``land`` approval records
        ``approved``. A rejection is always ``rejected``. Raises an
        :class:`~herald.approvals.ApprovalError` when the token is unknown, already used or
        expired, so the caller can send a new request instead of failing silently.
        """
        approval = self._store.redeem(reply.token)
        # The approval's action decides what "approved" means: a ``run`` gate releases the
        # task to be executed (ACTION -> QUEUED); a ``land`` gate records the decision
        # (ACTION -> APPROVED). A rejection is always terminal.
        if reply.decision != "approved":
            target = TaskState.REJECTED
        elif approval.action == "run":
            target = TaskState.QUEUED
        else:
            target = TaskState.APPROVED

        task = self._queue.get(approval.task_id)
        if task is None:
            raise UnknownTokenError(reply.token)
        return self._queue.transition(task, target)


__all__ = [
    "ApprovalService",
    "ApprovalReply",
    "parse_reply",
    "ApprovalStore",
    "ExpiredTokenError",
    "TokenAlreadyUsedError",
    "UnknownTokenError",
]

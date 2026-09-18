from __future__ import annotations

import re
from dataclasses import dataclass

from herald.approvals import (
    ApprovalStore,
    ExpiredTokenError,
    TokenAlreadyUsedError,
    UnknownTokenError,
)
from herald.queue.models import TaskState
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

    def apply(self, reply: ApprovalReply) -> str:
        """Redeem ``reply`` and move its task to the matching terminal state.

        Raises an :class:`~herald.approvals.ApprovalError` when the token is unknown,
        already used or expired, so the caller can send a new request instead of failing
        silently.
        """
        approval = self._store.redeem(reply.token)
        target = TaskState.APPROVED if reply.decision == "approved" else TaskState.REJECTED

        task = self._queue.get(approval.task_id)
        if task is None:
            raise UnknownTokenError(reply.token)
        transitioned = self._queue.transition(task, target)
        return transitioned.id


__all__ = [
    "ApprovalService",
    "ApprovalReply",
    "parse_reply",
    "ApprovalStore",
    "ExpiredTokenError",
    "TokenAlreadyUsedError",
    "UnknownTokenError",
]

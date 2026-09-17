from __future__ import annotations

import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

TOKEN_BYTES = 16
DEFAULT_TTL = timedelta(days=1)


def utcnow() -> datetime:
    return datetime.now(UTC)


class ApprovalError(Exception):
    """Base class for approval errors."""


class UnknownTokenError(ApprovalError):
    """The reply referenced a token that was never issued."""


class ExpiredTokenError(ApprovalError):
    """The token exists but is past its expiry."""


class TokenAlreadyUsedError(ApprovalError):
    """The token was already consumed; single-use is enforced."""


@dataclass(slots=True)
class Approval:
    """A pending human decision for one task."""

    token: str
    task_id: str
    action: str
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None

    @property
    def used(self) -> bool:
        return self.used_at is not None

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


@runtime_checkable
class ApprovalStore(Protocol):
    """Store for issued approval tokens.

    A token cannot live on the immutable email, so it is kept beside the queue. The
    interface hides whether that is memory or a file.
    """

    def issue(self, task_id: str, action: str) -> Approval: ...
    def get(self, token: str) -> Approval | None: ...
    def redeem(self, token: str) -> Approval: ...
    def pending(self) -> list[Approval]: ...


class _ApprovalRegistry:
    """Shared issue/redeem logic; subclasses decide how to persist."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = utcnow,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._by_token: dict[str, Approval] = {}

    def issue(self, task_id: str, action: str) -> Approval:
        now = self._clock()
        approval = Approval(
            token=secrets.token_urlsafe(TOKEN_BYTES),
            task_id=task_id,
            action=action,
            created_at=now,
            expires_at=now + self._ttl,
        )
        self._by_token[approval.token] = approval
        self._persist()
        return approval

    def get(self, token: str) -> Approval | None:
        return self._by_token.get(token)

    def redeem(self, token: str) -> Approval:
        approval = self._by_token.get(token)
        if approval is None:
            raise UnknownTokenError(token)
        if approval.used:
            raise TokenAlreadyUsedError(token)
        if approval.expired(self._clock()):
            raise ExpiredTokenError(token)
        approval.used_at = self._clock()
        self._persist()
        return approval

    def pending(self) -> list[Approval]:
        return [approval for approval in self._by_token.values() if not approval.used]

    def _persist(self) -> None:
        """Hook for durable subclasses; memory does nothing."""


class MemoryApprovalStore(_ApprovalRegistry):
    """In-memory approval store for tests and single-process runs."""


class FileApprovalStore(_ApprovalRegistry):
    """JSON-file approval store for a long-lived process; keep the path out of git."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] = utcnow,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        self._path = Path(path)
        super().__init__(clock=clock, ttl=ttl)
        if self._path.exists():
            self._load()

    def _persist(self) -> None:
        payload = {
            token: {
                "task_id": approval.task_id,
                "action": approval.action,
                "created_at": approval.created_at.isoformat(),
                "expires_at": approval.expires_at.isoformat(),
                "used_at": approval.used_at.isoformat() if approval.used_at else None,
            }
            for token, approval in self._by_token.items()
        }
        self._path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    def _load(self) -> None:
        raw = json.loads(self._path.read_text())
        for token, entry in raw.items():
            self._by_token[token] = Approval(
                token=token,
                task_id=entry["task_id"],
                action=entry["action"],
                created_at=datetime.fromisoformat(entry["created_at"]),
                expires_at=datetime.fromisoformat(entry["expires_at"]),
                used_at=datetime.fromisoformat(entry["used_at"]) if entry["used_at"] else None,
            )


def tokens_equal(left: str, right: str) -> bool:
    """Constant-time comparison, used when a token travels over an untrusted channel."""
    return hmac.compare_digest(left, right)

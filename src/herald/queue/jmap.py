from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from herald.jmap.client import EmailRecord, StateMismatchError
from herald.queue.models import Task, TaskState, utcnow
from herald.queue.port import StateConflictError, TaskNotFoundError

STATE_KEYWORDS: dict[TaskState, str] = {
    TaskState.QUEUED: "$herald-queued",
    TaskState.RUNNING: "$herald-running",
    TaskState.ACTION: "$herald-action",
    TaskState.DONE: "$herald-done",
    TaskState.FAILED: "$herald-failed",
    TaskState.REJECTED: "$herald-rejected",
    TaskState.APPROVED: "$herald-approved",
}
KEYWORD_STATES: dict[str, TaskState] = {keyword: state for state, keyword in STATE_KEYWORDS.items()}
ATTEMPT_PREFIX = "$herald-attempt-"
LEASE_PREFIX = "$herald-lease-"


class MailClient(Protocol):
    """The slice of :class:`~herald.jmap.client.JmapClient` the queue depends on."""

    def query_ids(self, *, filter: dict[str, Any], limit: int) -> list[str]: ...
    def get(self, ids: list[str]) -> tuple[str, list[EmailRecord]]: ...
    def update(self, email_id: str, patch: dict[str, Any], *, if_in_state: str) -> str: ...
    def get_or_create_mailbox(self, name: str) -> str: ...


class JmapQueue:
    """Queue over a JMAP mailbox: state in keywords, tasks in a single mailbox.

    Claiming and transitions are guarded with ``ifInState`` so concurrent workers cannot
    take the same task. The lease deadline and attempt count are encoded in keywords
    because a JMAP email has no other mutable field.
    """

    def __init__(
        self,
        client: MailClient,
        *,
        mailbox_name: str = "Herald",
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._client = client
        self._clock = clock
        self._mailbox_name = mailbox_name
        self._mailbox_id: str | None = None

    # Queue port ---------------------------------------------------------

    def enqueue(self, task: Task) -> Task | None:
        # Two dedupe layers:
        #  1. a query by Message-ID catches re-delivery that produced a second Email; only a
        #     message that already carries a Herald state keyword counts, otherwise the raw
        #     inbound message itself would match and the task could never be enqueued;
        #  2. the guarded write below closes the concurrency window for the same Email,
        #     because two racing enqueues read the same state and only one `ifInState`
        #     write can win. A lost race is a duplicate, never a second task.
        existing = self._client.query_ids(
            filter={
                "inMailbox": self._mailbox(),
                "header": ["Message-ID", task.transport_id],
            },
            limit=5,
        )
        if existing:
            _, records = self._client.get(existing)
            # Only a message that reached a Herald state is a duplicate; the raw inbound
            # message (still `${RECEIVED}`) is the one we are here to enqueue.
            if any(self._state_of(record) is not TaskState.RECEIVED for record in records):
                return None

        # The task id from the Normalizer is the transport Message-ID, but JMAP mutations
        # need the Email id. Resolve it from the Message-ID when the id is not already one.
        state, records = self._client.get([task.id])
        if not records and existing:
            state, records = self._client.get([existing[0]])
        if not records:
            raise TaskNotFoundError(task.id)
        record = records[0]

        if self._state_of(record) is TaskState.QUEUED:
            return None

        patch = self._state_patch(record, TaskState.QUEUED)
        patch.update(self._clear_prefix(record, ATTEMPT_PREFIX))
        patch.update(self._clear_prefix(record, LEASE_PREFIX))
        patch[f"mailboxIds/{self._mailbox()}"] = True
        try:
            self._client.update(record.id, patch, if_in_state=state)
        except StateMismatchError:
            return None

        return self._task_from(record, state=TaskState.QUEUED, attempts=0, lease_until=None)

    def get(self, task_id: str) -> Task | None:
        _, records = self._client.get([task_id])
        return self._task_from(records[0]) if records else None

    def list(self, state: TaskState, *, limit: int = 10) -> list[Task]:
        keyword = STATE_KEYWORDS.get(state)
        if keyword is None:
            return []
        ids = self._client.query_ids(
            filter={"inMailbox": self._mailbox(), "hasKeyword": keyword}, limit=limit
        )
        if not ids:
            return []
        _, records = self._client.get(ids)
        tasks = [self._task_from(record) for record in records]
        tasks.sort(key=lambda task: (task.created_at, task.id))
        return tasks

    def claim(self, task: Task, *, lease: timedelta) -> bool:
        state, records = self._client.get([task.id])
        if not records:
            return False
        record = records[0]
        if self._state_of(record) is not TaskState.QUEUED:
            return False

        lease_until = self._clock() + lease
        patch = self._state_patch(record, TaskState.RUNNING)
        patch.update(self._clear_prefix(record, LEASE_PREFIX))
        patch[f"keywords/{LEASE_PREFIX}{int(lease_until.timestamp())}"] = True
        try:
            self._client.update(task.id, patch, if_in_state=state)
        except StateMismatchError:
            return False
        return True

    def transition(self, task: Task, to_state: TaskState) -> Task:
        state, records = self._client.get([task.id])
        if not records:
            raise TaskNotFoundError(task.id)
        record = records[0]
        current = self._state_of(record)
        if current is not task.state:
            raise StateConflictError(task.id, task.state, current)

        patch = self._state_patch(record, to_state)
        if to_state is not TaskState.RUNNING:
            patch.update(self._clear_prefix(record, LEASE_PREFIX))
        try:
            self._client.update(task.id, patch, if_in_state=state)
        except StateMismatchError as exc:
            raise StateConflictError(task.id, task.state, current) from exc
        return self._task_from(
            record,
            state=to_state,
            lease_until=None if to_state is not TaskState.RUNNING else self._lease_of(record),
        )

    def release_stale(self, *, now: datetime) -> list[Task]:
        ids = self._client.query_ids(
            filter={
                "inMailbox": self._mailbox(),
                "hasKeyword": STATE_KEYWORDS[TaskState.RUNNING],
            },
            limit=100,
        )
        released: list[Task] = []
        for email_id in ids:
            state, records = self._client.get([email_id])
            if not records:
                continue
            record = records[0]
            lease = self._lease_of(record)
            if lease is None or lease >= now:
                continue

            attempts = self._attempts_of(record) + 1
            patch = self._state_patch(record, TaskState.QUEUED)
            patch.update(self._clear_prefix(record, LEASE_PREFIX))
            patch.update(self._clear_prefix(record, ATTEMPT_PREFIX))
            patch[f"keywords/{ATTEMPT_PREFIX}{attempts}"] = True
            try:
                self._client.update(email_id, patch, if_in_state=state)
            except StateMismatchError:
                continue
            released.append(
                self._task_from(record, state=TaskState.QUEUED, attempts=attempts, lease_until=None)
            )

        released.sort(key=lambda task: (task.created_at, task.id))
        return released

    # Internals ----------------------------------------------------------

    def _mailbox(self) -> str:
        if self._mailbox_id is None:
            self._mailbox_id = self._client.get_or_create_mailbox(self._mailbox_name)
        return self._mailbox_id

    @staticmethod
    def _state_patch(record: EmailRecord, to_state: TaskState) -> dict[str, Any]:
        target = STATE_KEYWORDS[to_state]
        patch: dict[str, Any] = {f"keywords/{target}": True}
        for keyword in record.keywords:
            if keyword in KEYWORD_STATES and keyword != target:
                patch[f"keywords/{keyword}"] = None
        return patch

    @staticmethod
    def _clear_prefix(record: EmailRecord, prefix: str) -> dict[str, Any]:
        return {
            f"keywords/{keyword}": None for keyword in record.keywords if keyword.startswith(prefix)
        }

    def _task_from(
        self,
        record: EmailRecord,
        *,
        state: TaskState | None = None,
        attempts: int | None = None,
        lease_until: datetime | None = None,
    ) -> Task:
        return Task(
            id=record.id,
            transport_id=record.message_id,
            thread_id=record.thread_id,
            sender=record.sender,
            subject=record.subject,
            state=self._state_of(record) if state is None else state,
            attempts=self._attempts_of(record) if attempts is None else attempts,
            lease_until=self._lease_of(record) if lease_until is None else lease_until,
            created_at=record.received_at,
        )

    @staticmethod
    def _state_of(record: EmailRecord) -> TaskState:
        for keyword in record.keywords:
            if keyword in KEYWORD_STATES:
                return KEYWORD_STATES[keyword]
        return TaskState.RECEIVED

    @staticmethod
    def _attempts_of(record: EmailRecord) -> int:
        attempts = 0
        for keyword in record.keywords:
            if keyword.startswith(ATTEMPT_PREFIX):
                suffix = keyword[len(ATTEMPT_PREFIX) :]
                if suffix.isdigit():
                    attempts = max(attempts, int(suffix))
        return attempts

    @staticmethod
    def _lease_of(record: EmailRecord) -> datetime | None:
        for keyword in record.keywords:
            if keyword.startswith(LEASE_PREFIX):
                suffix = keyword[len(LEASE_PREFIX) :]
                if suffix.isdigit():
                    return datetime.fromtimestamp(int(suffix), tz=UTC)
        return None

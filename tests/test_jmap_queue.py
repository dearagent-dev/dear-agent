from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from herald.jmap.client import EmailRecord, StateMismatchError
from herald.queue.jmap import LEASE_PREFIX, JmapQueue
from herald.queue.models import Task, TaskState
from herald.queue.port import Queue, StateConflictError

START = datetime(2026, 1, 1, tzinfo=UTC)
LEASE = timedelta(hours=1)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass
class FakeEmail:
    id: str
    message_id: str
    thread_id: str | None = None
    sender: str | None = None
    subject: str | None = None
    received_at: datetime = START
    keywords: set[str] = field(default_factory=set)
    mailbox_ids: set[str] = field(default_factory=set)


class FakeJmapMailClient:
    def __init__(self) -> None:
        self.emails: dict[str, FakeEmail] = {}
        self.fail_next_update = False
        self._version = 0
        self._mailbox_id = "mbx:Herald"

    @property
    def state(self) -> str:
        return f"S{self._version}"

    def add(self, email: FakeEmail) -> FakeEmail:
        self.emails[email.id] = email
        return email

    def query_ids(self, *, filter: dict, limit: int) -> list[str]:
        mailbox = filter.get("inMailbox")
        header = filter.get("header")
        keyword = filter.get("hasKeyword")
        matched = []
        for email in self.emails.values():
            if mailbox is not None and mailbox not in email.mailbox_ids:
                continue
            if header is not None and (header[0] != "Message-ID" or email.message_id != header[1]):
                continue
            if keyword is not None and keyword not in email.keywords:
                continue
            matched.append(email)
        matched.sort(key=lambda email: (email.received_at, email.id))
        return [email.id for email in matched[:limit]]

    def get(self, ids: list[str]) -> tuple[str, list[EmailRecord]]:
        records = []
        for email_id in ids:
            email = self.emails.get(email_id)
            if email is None:
                continue
            records.append(
                EmailRecord(
                    id=email.id,
                    message_id=email.message_id,
                    thread_id=email.thread_id,
                    sender=email.sender,
                    subject=email.subject,
                    received_at=email.received_at,
                    keywords=set(email.keywords),
                    mailbox_ids=set(email.mailbox_ids),
                )
            )
        return self.state, records

    def update(self, email_id: str, patch: dict, *, if_in_state: str) -> str:
        if self.fail_next_update or if_in_state != self.state:
            self.fail_next_update = False
            raise StateMismatchError("stale")
        email = self.emails[email_id]
        for key, value in patch.items():
            if key.startswith("keywords/"):
                keyword = key[len("keywords/") :]
                if value:
                    email.keywords.add(keyword)
                else:
                    email.keywords.discard(keyword)
            elif key.startswith("mailboxIds/"):
                mailbox = key[len("mailboxIds/") :]
                if value:
                    email.mailbox_ids.add(mailbox)
                else:
                    email.mailbox_ids.discard(mailbox)
        self._version += 1
        return self.state

    def get_or_create_mailbox(self, name: str) -> str:
        return self._mailbox_id


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture()
def client() -> FakeJmapMailClient:
    return FakeJmapMailClient()


@pytest.fixture()
def queue(client: FakeJmapMailClient, clock: FakeClock) -> JmapQueue:
    return JmapQueue(client, clock=clock)


def add_email(
    client: FakeJmapMailClient,
    email_id: str = "e1",
    message_id: str = "<m1@x>",
    *,
    sender: str | None = None,
    thread_id: str | None = None,
) -> FakeEmail:
    return client.add(
        FakeEmail(
            id=email_id,
            message_id=message_id,
            thread_id=thread_id,
            sender=sender,
            subject="task",
        )
    )


def test_jmap_queue_satisfies_the_port(queue: JmapQueue) -> None:
    assert isinstance(queue, Queue)


def test_enqueue_marks_queued_and_is_idempotent(
    queue: JmapQueue, client: FakeJmapMailClient
) -> None:
    add_email(client)

    stored = queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    duplicate = queue.enqueue(Task(id="e2", transport_id="<m1@x>"))

    assert stored is not None
    assert stored.state is TaskState.QUEUED
    assert duplicate is None


def test_enqueue_clears_previous_state_keywords(
    queue: JmapQueue, client: FakeJmapMailClient
) -> None:
    email = add_email(client)
    email.keywords.update({"$herald-running", "$herald-attempt-2", "$herald-lease-123"})

    queue.enqueue(Task(id="e1", transport_id="<m1@x>"))

    assert "$herald-queued" in email.keywords
    assert "$herald-running" not in email.keywords
    assert not any(keyword.startswith("$herald-attempt-") for keyword in email.keywords)
    assert not any(keyword.startswith(LEASE_PREFIX) for keyword in email.keywords)


def test_enqueue_ignores_a_raw_message_with_the_same_message_id(
    queue: JmapQueue, client: FakeJmapMailClient
) -> None:
    # The inbound message that produced the task is already in the mailbox (so a Message-ID
    # query matches it) but carries no Herald state keyword. It must not block enqueueing.
    add_email(client, message_id="<m1@x>")

    stored = queue.enqueue(Task(id="e1", transport_id="<m1@x>"))

    assert stored is not None
    assert stored.state is TaskState.QUEUED


def test_enqueue_dedupes_a_message_already_carrying_a_herald_state(
    queue: JmapQueue, client: FakeJmapMailClient
) -> None:
    first = add_email(client, email_id="e1", message_id="<m1@x>")
    first.mailbox_ids.add(client._mailbox_id)
    first.keywords.add("$herald-queued")
    # A redelivery arrives as a second Email with the same Message-ID.
    second = add_email(client, email_id="e2", message_id="<m1@x>")
    second.mailbox_ids.add(client._mailbox_id)

    assert queue.enqueue(Task(id="e2", transport_id="<m1@x>")) is None


def test_enqueue_losing_the_ifinstate_race_is_a_noop(
    queue: JmapQueue, client: FakeJmapMailClient
) -> None:
    add_email(client, "e1", "<m1@x>")
    add_email(client, "e2", "<m2@x>")

    original_update = client.update
    calls = {"n": 0}

    def racing_update(email_id: str, patch: dict, *, if_in_state: str) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            client._version += 1
        return original_update(email_id, patch, if_in_state=if_in_state)

    client.update = racing_update  # type: ignore[method-assign]
    first = queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    second = queue.enqueue(Task(id="e2", transport_id="<m2@x>"))

    assert first is not None
    assert second is None
    assert len(queue.list(TaskState.QUEUED)) == 1


def test_list_filters_by_state(queue: JmapQueue, client: FakeJmapMailClient) -> None:
    add_email(client, "e1", "<m1@x>")
    add_email(client, "e2", "<m2@x>")
    queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    queue.enqueue(Task(id="e2", transport_id="<m2@x>"))

    assert [task.id for task in queue.list(TaskState.QUEUED)] == ["e1", "e2"]
    assert queue.list(TaskState.RUNNING) == []


def test_claim_sets_the_lease_keyword(
    queue: JmapQueue, client: FakeJmapMailClient, clock: FakeClock
) -> None:
    email = add_email(client, sender="dev@example.com", thread_id="t1")
    task = queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    assert task is not None

    assert queue.claim(task, lease=LEASE) is True

    claimed = queue.get("e1")
    assert claimed.state is TaskState.RUNNING
    assert claimed.sender == "dev@example.com"
    assert claimed.lease_until == START + LEASE
    assert f"{LEASE_PREFIX}{int((START + LEASE).timestamp())}" in email.keywords


def test_claim_returns_false_when_not_queued(queue: JmapQueue, client: FakeJmapMailClient) -> None:
    add_email(client)
    task = queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    assert task is not None
    queue.claim(task, lease=LEASE)

    assert queue.claim(task, lease=LEASE) is False


def test_claim_returns_false_on_state_mismatch(
    queue: JmapQueue, client: FakeJmapMailClient
) -> None:
    add_email(client)
    task = queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    assert task is not None
    client.fail_next_update = True

    assert queue.claim(task, lease=LEASE) is False


def test_transition_raises_on_state_conflict(queue: JmapQueue, client: FakeJmapMailClient) -> None:
    add_email(client)
    task = queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    assert task is not None
    queue.claim(task, lease=LEASE)

    with pytest.raises(StateConflictError):
        queue.transition(task, TaskState.DONE)


def test_release_stale_requeues_and_bumps_attempts(
    queue: JmapQueue, client: FakeJmapMailClient, clock: FakeClock
) -> None:
    email = add_email(client)
    task = queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    assert task is not None
    queue.claim(task, lease=LEASE)

    clock.advance(LEASE + timedelta(seconds=1))
    released = queue.release_stale(now=clock.now)

    assert [released_task.id for released_task in released] == ["e1"]
    assert queue.get("e1").attempts == 1
    assert queue.get("e1").state is TaskState.QUEUED
    assert "$herald-attempt-1" in email.keywords
    assert not any(keyword.startswith(LEASE_PREFIX) for keyword in email.keywords)

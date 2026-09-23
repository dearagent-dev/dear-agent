from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from dear_agent.approvals import (
    ApprovalStore,
    ExpiredTokenError,
    PostgresApprovalStore,
    TokenAlreadyUsedError,
    UnknownTokenError,
)

DSN = os.environ.get("DEAR_AGENT_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="DEAR_AGENT_TEST_DATABASE_URL is not set")

START = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture()
def conn():
    pytest.importorskip("psycopg")
    from dear_agent.db import connect, init_schema

    connection = connect(DSN)
    init_schema(connection)
    with connection.cursor() as cur:
        cur.execute("TRUNCATE dear_agent_approval")
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def store(conn, clock: FakeClock) -> PostgresApprovalStore:
    return PostgresApprovalStore(conn, clock=clock)


def test_postgres_approval_store_satisfies_the_port(store) -> None:
    assert isinstance(store, ApprovalStore)


def test_issue_then_redeem_marks_used(store) -> None:
    approval = store.issue("e1", "land")

    assert approval.used is False
    assert store.redeem(approval.token).used is True
    assert store.get(approval.token).used is True


def test_single_use_token_cannot_be_redeemed_twice(store) -> None:
    approval = store.issue("e1", "land")
    store.redeem(approval.token)

    with pytest.raises(TokenAlreadyUsedError):
        store.redeem(approval.token)


def test_unknown_token_is_rejected(store) -> None:
    with pytest.raises(UnknownTokenError):
        store.redeem("nope")


def test_expired_token_is_rejected(store, clock: FakeClock) -> None:
    approval = store.issue("e1", "land")
    clock.advance(timedelta(days=2))

    with pytest.raises(ExpiredTokenError):
        store.redeem(approval.token)
    # An expired token is not silently consumed.
    assert store.get(approval.token).used is False


def test_pending_excludes_used(store) -> None:
    first = store.issue("e1", "land")
    store.issue("e2", "land")
    store.redeem(first.token)

    assert [approval.task_id for approval in store.pending()] == ["e2"]


def test_token_is_shared_across_connections(clock: FakeClock) -> None:
    # The point of the database store: the control plane issues, another process redeems.
    pytest.importorskip("psycopg")
    from dear_agent.db import connect

    issuer = PostgresApprovalStore(connect(DSN), clock=clock)
    redeemer_conn = connect(DSN)
    redeemer = PostgresApprovalStore(redeemer_conn, clock=clock)
    try:
        approval = issuer.issue("e1", "land")

        assert redeemer.get(approval.token) is not None
        assert redeemer.redeem(approval.token).used is True
    finally:
        redeemer_conn.close()

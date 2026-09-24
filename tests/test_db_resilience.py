from __future__ import annotations

from typing import Any

import psycopg

from dear_agent.db import ResilientConnection


class FakeCursor:
    def __init__(self, *, fail_first: bool = False) -> None:
        self._fail_first = fail_first
        self.executed: list[str] = []

    def execute(self, sql: str, params: Any = None) -> str:
        if self._fail_first:
            self._fail_first = False
            raise psycopg.OperationalError("connection lost")
        self.executed.append(sql)
        return sql

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeConnection:
    def __init__(self, *, fail_first_statement: bool = False) -> None:
        self.closed = False
        self.commits = 0
        self._fail_first_statement = fail_first_statement

    def cursor(self, *args: Any, **kwargs: Any) -> FakeCursor:
        cursor = FakeCursor(fail_first=self._fail_first_statement)
        self._fail_first_statement = False
        return cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True


class Opener:
    def __init__(self, *, fail_first_statement: bool = False) -> None:
        self._fail_first = fail_first_statement
        self.opened: list[FakeConnection] = []

    def __call__(self, dsn: str) -> FakeConnection:
        conn = FakeConnection(fail_first_statement=self._fail_first)
        self._fail_first = False
        self.opened.append(conn)
        return conn


def test_connect_returns_a_resilient_connection() -> None:
    from dear_agent.db import connect

    assert isinstance(connect("postgresql://x"), ResilientConnection)


def test_reconnects_after_the_connection_is_closed() -> None:
    opener = Opener()
    conn = ResilientConnection("dsn", opener=opener)

    with conn.cursor() as cur:
        cur.execute("SELECT 1")
    conn._conn.close()  # the server dropped the connection
    with conn.cursor() as cur:
        cur.execute("SELECT 1")

    assert len(opener.opened) == 2


def test_retries_a_statement_once_after_an_operational_error() -> None:
    opener = Opener(fail_first_statement=True)
    conn = ResilientConnection("dsn", opener=opener)

    with conn.cursor() as cur:
        assert cur.execute("SELECT 1") == "SELECT 1"

    assert len(opener.opened) == 2  # the failed connection, then a fresh one


def test_commit_and_close_delegate() -> None:
    opener = Opener()
    conn = ResilientConnection("dsn", opener=opener)

    conn.commit()
    assert opener.opened[0].commits == 1

    conn.close()
    assert conn.closed is True

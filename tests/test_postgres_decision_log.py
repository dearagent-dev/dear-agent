from __future__ import annotations

import os

import pytest

from dear_agent.decision.log import DecisionRecord, PostgresDecisionLog

DSN = os.environ.get("DEAR_AGENT_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="DEAR_AGENT_TEST_DATABASE_URL is not set")


def _record(choice: str, confidence: float, question_id: str = "model") -> DecisionRecord:
    return DecisionRecord(
        at="2026-01-01T00:00:00+00:00",
        state="task",
        questions={question_id: {"kind": "choice", "instructions": "?", "criteria": None}},
        answers={
            question_id: {
                "kind": "choice",
                "choice": choice,
                "score": None,
                "noul": None,
                "confidence": confidence,
                "probabilities": {},
            }
        },
    )


@pytest.fixture()
def log():
    pytest.importorskip("psycopg")
    from dear_agent.db import connect, init_schema

    conn = connect(DSN)
    init_schema(conn)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE dear_agent_decision")
    conn.commit()
    yield PostgresDecisionLog(conn)
    conn.close()


def test_record_and_read_round_trip(log) -> None:
    log.record(_record("hosted", 0.9))
    log.record(_record("local", 0.8))

    records = log.read()

    assert [record.answers["model"]["choice"] for record in records] == ["hosted", "local"]
    assert records[0].state == "task"
    assert records[0].label is None


def test_label_sets_the_ground_truth(log) -> None:
    log.record(_record("hosted", 0.9))
    log.record(_record("local", 0.8))

    assert log.label(1, "local") is True

    assert [record.label for record in log.read()] == [None, "local"]


def test_label_rejects_an_out_of_range_index(log) -> None:
    log.record(_record("hosted", 0.9))

    assert log.label(5, "hosted") is False


def test_calibration_reports_accuracy_per_band(log) -> None:
    for choice, confidence, label in [
        ("hosted", 0.95, "hosted"),
        ("hosted", 0.90, "local"),
        ("local", 0.55, "local"),
        ("hosted", 0.55, "local"),
    ]:
        log.record(_record(choice, confidence))
        log.label(len(log.read()) - 1, label)

    report = log.calibrate(question_id="model", bands=(0.5, 0.8))

    assert report.total == 4
    assert report.labeled == 4
    assert report.accuracy == pytest.approx(0.5)

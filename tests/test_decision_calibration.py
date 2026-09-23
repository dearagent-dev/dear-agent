from __future__ import annotations

import pytest

from dear_agent.decision.log import DecisionLog, DecisionRecord


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


def test_label_sets_the_ground_truth_on_a_record(tmp_path) -> None:
    log = DecisionLog(path=tmp_path / "d.jsonl")
    log.record(_record("hosted", 0.9))
    log.record(_record("local", 0.8))

    assert log.label(1, "local") is True
    records = log.read()
    assert records[0].label is None
    assert records[1].label == "local"


def test_label_rejects_an_out_of_range_index(tmp_path) -> None:
    log = DecisionLog(path=tmp_path / "d.jsonl")
    log.record(_record("hosted", 0.9))

    assert log.label(5, "hosted") is False


def test_calibration_reports_accuracy_per_confidence_band(tmp_path) -> None:
    log = DecisionLog(path=tmp_path / "d.jsonl")
    # confident + right, confident + wrong, unconfident + right, unconfident + wrong
    for choice, conf, label in [
        ("hosted", 0.95, "hosted"),
        ("hosted", 0.90, "local"),
        ("local", 0.55, "local"),
        ("hosted", 0.55, "local"),
    ]:
        log.record(_record(choice, conf))
        log.label(len(log.read()) - 1, label)

    report = log.calibrate(question_id="model", bands=(0.5, 0.8))

    assert report.total == 4
    assert report.labeled == 4
    assert report.accuracy == pytest.approx(0.5)
    low, high = report.bands
    assert low.label == "0.50-0.80"
    assert low.count == 2
    assert low.accuracy == pytest.approx(0.5)
    assert high.label == "0.80-1.00"
    assert high.count == 2
    assert high.accuracy == pytest.approx(0.5)


def test_calibration_recommends_a_threshold_meeting_a_target(tmp_path) -> None:
    log = DecisionLog(path=tmp_path / "d.jsonl")
    # 0.9+ always right; 0.5-0.9 always wrong.
    for choice, conf, label in [
        ("hosted", 0.95, "hosted"),
        ("hosted", 0.92, "hosted"),
        ("local", 0.70, "hosted"),
        ("local", 0.60, "hosted"),
    ]:
        log.record(_record(choice, conf))
        log.label(len(log.read()) - 1, label)

    report = log.calibrate(question_id="model", bands=(0.5, 0.9), target=1.0)

    assert report.recommended_threshold == 0.9


def test_calibration_ignores_unlabeled_records(tmp_path) -> None:
    log = DecisionLog(path=tmp_path / "d.jsonl")
    log.record(_record("hosted", 0.9))
    log.record(_record("hosted", 0.9))
    log.label(0, "hosted")

    report = log.calibrate(question_id="model")

    assert report.total == 2
    assert report.labeled == 1
    assert report.accuracy == pytest.approx(1.0)


def test_calibration_with_no_data_is_empty_not_an_error(tmp_path) -> None:
    report = DecisionLog(path=tmp_path / "d.jsonl").calibrate()

    assert report.total == 0
    assert report.labeled == 0
    assert report.accuracy is None
    assert report.recommended_threshold is None

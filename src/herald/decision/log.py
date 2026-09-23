from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from herald.decision.port import Decision, Question


@dataclass(slots=True)
class DecisionRecord:
    """One decision, kept for threshold tuning (ADR 0004, M7.4).

    Decisions are a training signal: without labels we cannot earn a cutoff. Records carry
    the state, the questions, the answers and their confidence, plus an optional human label
    added later. Trace must be trimmed by the caller — the log stores what it is given.
    """

    at: str
    state: str
    questions: dict[str, dict]
    answers: dict[str, dict]
    label: str | None = None

    @classmethod
    def from_decision(
        cls, state: str, questions: dict[str, Question], decision: Decision
    ) -> DecisionRecord:
        return cls(
            at=datetime.now(UTC).isoformat(),
            state=state,
            questions={
                question_id: {
                    "kind": question.kind.value,
                    "instructions": question.instructions,
                    "criteria": question.criteria,
                }
                for question_id, question in questions.items()
            },
            answers={
                question_id: asdict(answer) for question_id, answer in decision.answers.items()
            },
        )


@dataclass(slots=True)
class ConfidenceBand:
    """Accuracy within a confidence interval."""

    label: str
    lower: float
    upper: float
    count: int
    correct: int

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.count if self.count else None


@dataclass(slots=True)
class CalibrationReport:
    """How well a question's confidence tracks correctness (ADR 0004, M7.4).

    ``accuracy`` is over labeled records; ``bands`` show the accuracy inside each confidence
    interval; ``recommended_threshold`` is the lowest confidence whose band meets ``target``
    and keeps improving — the "earned" cutoff a caller should configure instead of guessing.
    """

    question_id: str
    total: int = 0
    labeled: int = 0
    correct: int = 0
    bands: list[ConfidenceBand] = field(default_factory=list)
    recommended_threshold: float | None = None

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.labeled if self.labeled else None


@dataclass(slots=True)
class DecisionLog:
    """Append-only JSONL log of decisions. No database; a plain file (ADR 0002 spirit)."""

    path: Path

    def record(self, record: DecisionRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), default=str) + "\n")

    def read(self) -> list[DecisionRecord]:
        if not self.path.exists():
            return []
        records: list[DecisionRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            records.append(
                DecisionRecord(
                    at=data["at"],
                    state=data["state"],
                    questions=data["questions"],
                    answers=data["answers"],
                    label=data.get("label"),
                )
            )
        return records

    def label(self, index: int, label: str) -> bool:
        """Set the ground-truth label on the record at ``index``. False if out of range.

        Labels are the missing half of a decision record: without them a log is only an
        audit trail, not a tuning signal. An operator (or a later review step) attaches the
        right answer here.
        """
        records = self.read()
        if not 0 <= index < len(records):
            return False
        records[index].label = label
        self._write_all(records)
        return True

    def calibrate(
        self,
        *,
        question_id: str = "model",
        bands: tuple[float, ...] = (0.5, 0.7, 0.9),
        target: float = 0.95,
    ) -> CalibrationReport:
        """Compute accuracy by confidence band and recommend a threshold (ADR 0004).

        For a ``choice`` question, a record is correct when its answer equals the label. Only
        labeled records with a choice answer and a confidence contribute.
        """
        edges = sorted(set(bands))
        report = CalibrationReport(question_id=question_id)
        buckets: list[list[tuple[float, bool]]] = [[] for _ in range(len(edges))]

        for record in self.read():
            answer = record.answers.get(question_id)
            if not isinstance(answer, dict):
                continue
            report.total += 1
            if record.label is None:
                continue
            confidence = answer.get("confidence")
            if confidence is None:
                continue
            report.labeled += 1
            correct = answer.get("choice") == record.label
            report.correct += correct
            buckets[_bucket_index(float(confidence), edges)].append((float(confidence), correct))

        for index, edge in enumerate(edges):
            upper = edges[index + 1] if index + 1 < len(edges) else 1.0
            entries = buckets[index]
            report.bands.append(
                ConfidenceBand(
                    label=f"{edge:.2f}-{upper:.2f}",
                    lower=edge,
                    upper=upper,
                    count=len(entries),
                    correct=sum(1 for _, ok in entries if ok),
                )
            )

        report.recommended_threshold = _recommend(edges, buckets, target)
        return report

    def _write_all(self, records: list[DecisionRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(asdict(record), default=str) + "\n")


def _bucket_index(confidence: float, edges: list[float]) -> int:
    index = 0
    for i, edge in enumerate(edges):
        if confidence >= edge:
            index = i
    return index


def _recommend(
    edges: list[float], buckets: list[list[tuple[float, bool]]], target: float
) -> float | None:
    """Lowest band edge whose accuracy meets ``target`` for it and every higher band."""
    for index in range(len(edges)):
        above = [entry for bucket in buckets[index:] for entry in bucket]
        if not above:
            continue
        accuracy = sum(1 for _, ok in above if ok) / len(above)
        if accuracy >= target:
            return edges[index]
    return None


__all__ = ["CalibrationReport", "ConfidenceBand", "DecisionLog", "DecisionRecord"]

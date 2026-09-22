from __future__ import annotations

import json
from dataclasses import asdict, dataclass
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


__all__ = ["DecisionLog", "DecisionRecord"]

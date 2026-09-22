from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class DecisionError(Exception):
    """A decision could not be obtained from the backing model."""


class DecisionKind(StrEnum):
    """The three System One question shapes (ADR 0004)."""

    CHOICE = "choice"
    SCORE = "score"
    NOUL = "noul"


@dataclass(slots=True, frozen=True)
class Question:
    """One typed question about a state.

    ``criteria`` is the label set for a ``choice`` (key -> meaning) or the ordered rubric for
    a ``score`` (low -> high). A ``noul`` needs only ``instructions``.
    """

    kind: DecisionKind
    instructions: str
    criteria: dict[str, str] | list[str] | None = None


@dataclass(slots=True, frozen=True)
class Answer:
    """A typed answer with its confidence.

    Exactly one of ``choice``/``score``/``noul`` is set, matching ``kind``. ``confidence`` is
    the model's own certainty (0-1); callers branch on it and fall back below their earned
    threshold. It is not a probability that the answer is correct.
    """

    kind: DecisionKind
    choice: str | None = None
    score: float | None = None
    noul: float | None = None
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class Decision:
    """The answers to a request, keyed by question id."""

    answers: dict[str, Answer] = field(default_factory=dict)

    def choice(self, question_id: str) -> str | None:
        answer = self.answers.get(question_id)
        return answer.choice if answer else None

    def score(self, question_id: str) -> float | None:
        answer = self.answers.get(question_id)
        return answer.score if answer else None

    def noul(self, question_id: str) -> float | None:
        answer = self.answers.get(question_id)
        return answer.noul if answer else None

    def confidence(self, question_id: str) -> float | None:
        answer = self.answers.get(question_id)
        return answer.confidence if answer else None


@runtime_checkable
class Decider(Protocol):
    """A decision model: state + typed questions in, typed answers out (ADR 0004).

    Implementations must be advisory: a caller may branch on a decision, but must never
    treat it as a security boundary, and must fall back to a deterministic rule when the
    decider is absent, unreachable, or unconfident.
    """

    def decide(self, state: str, questions: dict[str, Question]) -> Decision:
        """Return typed answers for ``state``. Raise :class:`DecisionError` on failure."""
        ...


__all__ = [
    "Answer",
    "Decider",
    "Decision",
    "DecisionError",
    "DecisionKind",
    "Question",
]

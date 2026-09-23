from __future__ import annotations

import re
from dataclasses import dataclass, field

from dear_agent.decision.port import Answer, Decision, DecisionKind, Question

# Deterministic content signals for the routing decision. Deliberately conservative: when
# nothing matches, we route "local" (the cheap path), and a caller can still escalate.
_HOSTED_SIGNALS: list[tuple[str, re.Pattern[str]]] = [
    (
        "architecture",
        re.compile(
            r"\b(refactor|architect|design|migrate|redesign|protocol|topolog|plugin)\w*",
            re.IGNORECASE,
        ),
    ),
    (
        "security",
        re.compile(
            r"\b(security|auth|vulnerab|timing|side-channel|injection|encrypt|harden|audit)\w*",
            re.IGNORECASE,
        ),
    ),
    (
        "debug",
        re.compile(r"\b(race|deadlock|memory leak|root cause|flak|investigat)\w*", re.IGNORECASE),
    ),
    (
        "infra",
        re.compile(r"\b(helm|terraform|deploy|kubernetes|openshift|migration)\w*", re.IGNORECASE),
    ),
    (
        "cross-cutting",
        re.compile(r"\b(rewrite|everywhere|all of|end to end|from scratch)\b", re.IGNORECASE),
    ),
]

_NON_TRIVIAL = re.compile(
    r"\b(implement|redesign|investigate|diagnose|migrate|harden|optimi[sz]e)\w*",
    re.IGNORECASE,
)


@dataclass(slots=True)
class RuleDecider:
    """A deterministic decider: the fallback when no model is configured (ADR 0004).

    It answers the decisions Dear Agent makes at the edges — which model class and whether a
    human should look — with plain rules, so the behavior with no decider is identical to
    today's. Confidence is always reported, so a caller's threshold logic is exercised even
    on the rule path.
    """

    hosted_signals: list[tuple[str, re.Pattern[str]]] = field(
        default_factory=lambda: list(_HOSTED_SIGNALS)
    )

    def decide(self, state: str, questions: dict[str, Question]) -> Decision:
        answers: dict[str, Answer] = {}
        for question_id, question in questions.items():
            # "harness" and "model" ask the same shape (cheap/light vs strong), so the rules
            # answer them identically; the caller maps the answer to its own targets.
            if question_id in {"model", "harness"}:
                answers[question_id] = self._route(state)
            elif question_id == "needs_human":
                answers[question_id] = self._needs_human(state)
            elif question_id == "complexity":
                answers[question_id] = self._complexity(state)
            else:
                answers[question_id] = self._default(question)
        return Decision(answers=answers)

    def _matches(self, state: str) -> list[str]:
        return [label for label, pattern in self.hosted_signals if pattern.search(state)]

    def _route(self, state: str) -> Answer:
        hits = self._matches(state)
        if hits:
            confidence = min(0.6 + 0.1 * len(hits), 0.95)
            return Answer(
                kind=DecisionKind.CHOICE,
                choice="hosted",
                confidence=confidence,
                probabilities={"hosted": confidence, "local": 1 - confidence},
            )
        return Answer(
            kind=DecisionKind.CHOICE,
            choice="local",
            confidence=0.7,
            probabilities={"local": 0.7, "hosted": 0.3},
        )

    def _needs_human(self, state: str) -> Answer:
        risky = {"security", "infra", "architecture", "cross-cutting"}
        if risky & set(self._matches(state)):
            return Answer(kind=DecisionKind.NOUL, noul=0.8, confidence=0.7)
        return Answer(kind=DecisionKind.NOUL, noul=0.2, confidence=0.7)

    def _complexity(self, state: str) -> Answer:
        if self._matches(state):
            return Answer(kind=DecisionKind.SCORE, score=3.0, confidence=0.6)
        if _NON_TRIVIAL.search(state):
            return Answer(kind=DecisionKind.SCORE, score=2.0, confidence=0.6)
        return Answer(kind=DecisionKind.SCORE, score=0.5, confidence=0.7)

    @staticmethod
    def _default(question: Question) -> Answer:
        if question.kind is DecisionKind.CHOICE and question.criteria:
            return Answer(
                kind=DecisionKind.CHOICE,
                choice=next(iter(question.criteria)),
                confidence=0.0,
            )
        if question.kind is DecisionKind.SCORE:
            return Answer(kind=DecisionKind.SCORE, score=0.0, confidence=0.0)
        return Answer(kind=DecisionKind.NOUL, noul=0.0, confidence=0.0)


__all__ = ["RuleDecider"]

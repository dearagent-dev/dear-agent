from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass

from herald.decision.log import DecisionLog, DecisionRecord
from herald.decision.port import Answer, Decider, Decision, DecisionError, DecisionKind, Question
from herald.decision.rules import RuleDecider
from herald.decision.selection import QUESTION_MODEL, QUESTION_NEEDS_HUMAN

DEFAULT_CONFIDENCE_THRESHOLD = 0.5


@dataclass(slots=True)
class FallbackDecider:
    """Wrap a primary decider so any failure or low confidence falls back to rules.

    This is the guarantee from ADR 0004: a decision model outage, a missing key, or an
    uncertain answer must never stop the queue or change behavior dangerously. Below the
    threshold the deterministic ``fallback`` answer is used.
    """

    primary: Decider
    fallback: Decider
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD

    def decide(self, state: str, questions: dict[str, Question]) -> Decision:
        try:
            decision = self.primary.decide(state, questions)
        except DecisionError:
            return self.fallback.decide(state, questions)
        uncertain = {
            question_id
            for question_id in questions
            if not _confident(decision, question_id, self.threshold)
        }
        if uncertain:
            decision = _replace(decision, self.fallback.decide(state, questions), uncertain)
        return decision


def _confident(decision: Decision, question_id: str, threshold: float) -> bool:
    answer = decision.answers.get(question_id)
    if answer is None:
        return False
    # noul/score carry no explicit confidence; treat a decisive split as confident enough.
    if answer.kind is DecisionKind.NOUL and answer.noul is not None:
        return abs(answer.noul - 0.5) >= (threshold - 0.5)
    if answer.confidence is None:
        return False
    return answer.confidence >= threshold


def _replace(primary: Decision, fallback: Decision, question_ids: set[str]) -> Decision:
    answers: dict[str, Answer] = dict(primary.answers)
    for question_id in question_ids:
        if question_id in fallback.answers:
            answers[question_id] = fallback.answers[question_id]
    return Decision(answers=answers)


@dataclass(slots=True)
class LoggingDecider:
    """Wrap a decider so every decision is appended to a log (ADR 0004, M7.4).

    The log is the raw material for earning thresholds; it is advisory metadata, never a
    control path, so a logging failure must not break the decision.
    """

    decider: Decider
    log: DecisionLog

    def decide(self, state: str, questions: dict[str, Question]) -> Decision:
        decision = self.decider.decide(state, questions)
        with contextlib.suppress(OSError):
            self.log.record(DecisionRecord.from_decision(state, questions, decision))
        return decision


def build_decider() -> Decider | None:
    """Build the configured decider, or ``None`` when none is configured.

    ``HERALD_DECIDER`` selects the primary: ``rules`` (default) uses only the deterministic
    path; ``jev`` wraps the hosted TypeSafe API; ``openrouter`` selects a model from
    OpenRouter's catalog by :class:`~herald.decision.selection.SelectionPolicy` (free first)
    and wraps the generic OpenAI-compatible decider. Every model-backed decider is wrapped in
    :class:`FallbackDecider`, so any error or low confidence falls back to the rules.
    """
    primary = os.environ.get("HERALD_DECIDER", "rules").lower()
    threshold = float(os.environ.get("HERALD_DECIDER_THRESHOLD", DEFAULT_CONFIDENCE_THRESHOLD))
    rules = RuleDecider()

    if primary == "none":
        return None
    if primary == "rules":
        return _maybe_log(rules)
    if primary == "jev":
        api_key = os.environ.get("TYPESAFE_API_KEY")
        if not api_key:
            raise DecisionError("TYPESAFE_API_KEY is required for HERALD_DECIDER=jev")
        from herald.decision.jev import DEFAULT_ENDPOINT, DEFAULT_MODEL, JevDecider

        return _maybe_log(
            FallbackDecider(
                primary=JevDecider(
                    api_key=api_key,
                    model=os.environ.get("HERALD_DECIDER_MODEL", DEFAULT_MODEL),
                    endpoint=os.environ.get("HERALD_DECIDER_ENDPOINT", DEFAULT_ENDPOINT),
                ),
                fallback=rules,
                threshold=threshold,
            )
        )
    if primary == "openrouter":
        return _maybe_log(_build_openrouter(threshold, rules))
    raise DecisionError(f"unknown decider {primary!r}")


def _build_openrouter(threshold: float, rules: Decider) -> Decider:
    from herald.decision.openrouter import OpenRouterProvider
    from herald.decision.selection import SelectionPolicy

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise DecisionError("OPENROUTER_API_KEY is required for HERALD_DECIDER=openrouter")
    provider = OpenRouterProvider(
        api_key=api_key,
        base_url=os.environ.get("HERALD_DECIDER_BASE_URL", "https://openrouter.ai/api/v1"),
        referer=os.environ.get("HERALD_DECIDER_REFERER"),
    )
    model_id = os.environ.get("HERALD_DECIDER_MODEL")
    if model_id:
        from herald.decision.port import ModelInfo

        model = ModelInfo(id=model_id, context_length=1_000_000, supports_json=True)
    else:
        policy = SelectionPolicy(
            min_context=int(os.environ.get("HERALD_DECIDER_MIN_CONTEXT", "8192")),
            allow_paid=os.environ.get("HERALD_DECIDER_ALLOW_PAID", "false").lower() == "true",
        )
        model = policy.select(provider.catalog())
    return FallbackDecider(primary=provider.decider_for(model), fallback=rules, threshold=threshold)


def _maybe_log(decider: Decider) -> Decider:
    path = os.environ.get("HERALD_DECIDER_LOG")
    if not path:
        return decider
    from pathlib import Path

    return LoggingDecider(decider=decider, log=DecisionLog(path=Path(path)))


__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "QUESTION_MODEL",
    "QUESTION_NEEDS_HUMAN",
    "FallbackDecider",
    "LoggingDecider",
    "build_decider",
]

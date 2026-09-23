from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from dear_agent.decision.port import Answer, Decision, DecisionError, DecisionKind, Question

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"

_KIND_TO_WIRE = {
    DecisionKind.CHOICE: "choice",
    DecisionKind.SCORE: "score",
    DecisionKind.NOUL: "noul",
}


@dataclass(slots=True)
class JevDecider:
    """A hosted System One decider over the TypeSafe API (ADR 0004).

    Stdlib-only, no vendor SDK. It is advisory and must be wrapped by a caller that falls
    back to :class:`~dear_agent.decision.rules.RuleDecider` on any error or low confidence. The
    API key is read from the environment by the factory, never stored on the instance.
    """

    api_key: str
    model: str = DEFAULT_MODEL
    endpoint: str = DEFAULT_ENDPOINT
    timeout: float = 30.0

    def decide(self, state: str, questions: dict[str, Question]) -> Decision:
        body = json.dumps(
            {"state": state, "model": self.model, "questions": self._render(questions)}
        ).encode()
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise DecisionError(f"jev {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise DecisionError(f"jev unreachable: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise DecisionError("jev returned non-JSON") from exc
        return self._parse(payload, questions)

    @staticmethod
    def _render(questions: dict[str, Question]) -> dict[str, dict]:
        rendered: dict[str, dict] = {}
        for question_id, question in questions.items():
            wire: dict = {
                "type": _KIND_TO_WIRE[question.kind],
                "instructions": question.instructions,
            }
            if question.criteria is not None:
                wire["criteria"] = question.criteria
            rendered[question_id] = wire
        return rendered

    @staticmethod
    def _parse(payload: dict, questions: dict[str, Question]) -> Decision:
        raw = payload.get("answers")
        if not isinstance(raw, dict):
            raise DecisionError(f"jev response has no answers: {payload}")
        answers: dict[str, Answer] = {}
        for question_id, question in questions.items():
            entry = raw.get(question_id)
            if not isinstance(entry, dict):
                raise DecisionError(f"jev omitted answer {question_id!r}")
            answers[question_id] = _answer_from(question.kind, entry)
        return Decision(answers=answers)


def _answer_from(kind: DecisionKind, entry: dict) -> Answer:
    confidence = entry.get("confidence")
    probabilities = entry.get("probabilities") or {}
    if kind is DecisionKind.CHOICE:
        return Answer(
            kind=kind,
            choice=entry.get("choice"),
            confidence=confidence,
            probabilities=probabilities,
        )
    if kind is DecisionKind.SCORE:
        return Answer(
            kind=kind, score=entry.get("score"), confidence=confidence, probabilities=probabilities
        )
    return Answer(kind=kind, noul=entry.get("noul"))


__all__ = ["DEFAULT_ENDPOINT", "DEFAULT_MODEL", "JevDecider"]

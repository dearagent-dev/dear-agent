from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from herald.decision.port import (
    Answer,
    Decision,
    DecisionError,
    DecisionKind,
    Question,
)

# The instruction that turns any OpenAI-compatible chat model into a typed decider. This is
# the "structured output" path (ADR 0004): unlike a System One model it generates text and we
# parse it, so validation is strict and any deviation raises DecisionError and falls back to
# the rules.
SYSTEM_PROMPT = (
    "You are a decision model, not a chat model. You receive a STATE and typed QUESTIONS and "
    "reply with ONE JSON object and nothing else. For every question, return an object with "
    "exactly the requested answer field.\n"
    '- choice: {"choice": <one of the listed keys>, "confidence": <0.0-1.0>}\n'
    '- score: {"score": <number within the rubric range, fractional allowed>, '
    '"confidence": <0.0-1.0>}\n'
    '- noul: {"noul": <probability the statement is true, 0.0-1.0>}\n'
    "Never invent options. Never add prose. Confidence is your own certainty, not the "
    "probability the answer is correct."
)


@dataclass(slots=True)
class OpenAICompatibleDecider:
    """A decider over any OpenAI-compatible chat endpoint (ADR 0004).

    Vendor-neutral: it sends ``POST {base_url}/chat/completions`` with a strict JSON
    instruction. Point it at a hosted API (OpenRouter, TypeSafe's adapter) or a local server
    (vLLM, llama.cpp). Stdlib-only and advisory; callers wrap it in a
    :class:`~herald.decision.factory.FallbackDecider` so a timeout, a rate limit, malformed
    JSON, or a missing/out-of-set answer falls back to the rules.
    """

    api_key: str
    model: str
    base_url: str
    timeout: float = 60.0
    extra_headers: dict[str, str] = field(default_factory=dict)

    def decide(self, state: str, questions: dict[str, Question]) -> Decision:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": self._user_prompt(state, questions)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "stream": False,
            }
        ).encode()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self.extra_headers,
        }
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise DecisionError(f"decider {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise DecisionError(f"decider unreachable: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise DecisionError("decider returned non-JSON") from exc
        return self._parse(payload, questions)

    @staticmethod
    def _user_prompt(state: str, questions: dict[str, Question]) -> str:
        schema: dict[str, dict] = {}
        for question_id, question in questions.items():
            entry: dict = {"type": question.kind.value, "instructions": question.instructions}
            if question.criteria is not None:
                entry["criteria"] = question.criteria
            schema[question_id] = entry
        return (
            "QUESTIONS (JSON):\n"
            + json.dumps(schema, indent=2)
            + "\n\nSTATE:\n"
            + state
            + "\n\nReply with one JSON object keyed by question id."
        )

    def _parse(self, payload: dict, questions: dict[str, Question]) -> Decision:
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DecisionError(f"decider response has no content: {payload}") from exc
        if not isinstance(content, str):
            raise DecisionError("decider content is not a string")
        try:
            data = json.loads(_strip_fence(content))
        except json.JSONDecodeError as exc:
            raise DecisionError(f"decider content is not JSON: {content[:200]!r}") from exc
        if not isinstance(data, dict):
            raise DecisionError("decider content is not a JSON object")

        answers: dict[str, Answer] = {}
        for question_id, question in questions.items():
            entry = data.get(question_id)
            if not isinstance(entry, dict):
                raise DecisionError(f"decider omitted answer {question_id!r}")
            answers[question_id] = _answer_from(question, entry)
        return Decision(answers=answers)


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()


def _answer_from(question: Question, entry: dict) -> Answer:
    confidence = _as_float(entry.get("confidence"))
    if question.kind is DecisionKind.CHOICE:
        choice = entry.get("choice")
        allowed = question.criteria if isinstance(question.criteria, dict) else None
        if not isinstance(choice, str) or (allowed is not None and choice not in allowed):
            raise DecisionError(f"choice {choice!r} is not one of {list(allowed or [])}")
        return Answer(kind=DecisionKind.CHOICE, choice=choice, confidence=confidence)
    if question.kind is DecisionKind.SCORE:
        score = _as_float(entry.get("score"))
        if score is None:
            raise DecisionError("score answer is missing a number")
        return Answer(kind=DecisionKind.SCORE, score=score, confidence=confidence)
    noul = _as_float(entry.get("noul"))
    if noul is None or not 0.0 <= noul <= 1.0:
        raise DecisionError(f"noul must be a probability in [0,1], got {entry.get('noul')!r}")
    return Answer(kind=DecisionKind.NOUL, noul=noul)


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


__all__ = [
    "SYSTEM_PROMPT",
    "OpenAICompatibleDecider",
]

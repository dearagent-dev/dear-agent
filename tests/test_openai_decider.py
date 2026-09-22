from __future__ import annotations

import json

import pytest

from herald.decision.openai import OpenAICompatibleDecider
from herald.decision.port import DecisionError, DecisionKind, Question

QUESTIONS = {
    "route": Question(
        kind=DecisionKind.CHOICE,
        instructions="which model class?",
        criteria={"local": "mechanical", "hosted": "reasoning"},
    ),
    "complexity": Question(
        kind=DecisionKind.SCORE,
        instructions="how complex?",
        criteria=["trivial", "simple", "moderate", "complex"],
    ),
    "needs_human": Question(kind=DecisionKind.NOUL, instructions="needs a human?"),
}

CHAT = {
    "choices": [
        {
            "message": {
                "content": json.dumps(
                    {
                        "route": {"choice": "hosted", "confidence": 0.9},
                        "complexity": {"score": 2.5, "confidence": 0.8},
                        "needs_human": {"noul": 0.75},
                    }
                )
            }
        }
    ]
}


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def _patch_urlopen(monkeypatch, payload: bytes) -> dict:
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data)
        return _Response(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return captured


def test_openai_decider_parses_a_strict_json_answer(monkeypatch) -> None:
    _patch_urlopen(monkeypatch, json.dumps(CHAT).encode())
    decider = OpenAICompatibleDecider(
        api_key="sk-test", model="qwen/qwen3:free", base_url="https://openrouter.ai/api/v1"
    )

    decision = decider.decide("refactor the queue port", QUESTIONS)

    assert decision.choice("route") == "hosted"
    assert decision.confidence("route") == 0.9
    assert decision.score("complexity") == 2.5
    assert decision.noul("needs_human") == 0.75


def test_openai_decider_sends_a_structured_request(monkeypatch) -> None:
    captured = _patch_urlopen(monkeypatch, json.dumps(CHAT).encode())
    decider = OpenAICompatibleDecider(
        api_key="sk-test", model="qwen/qwen3:free", base_url="https://openrouter.ai/api/v1"
    )

    decider.decide("refactor the queue port", QUESTIONS)

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["body"]["model"] == "qwen/qwen3:free"
    assert captured["body"]["response_format"]["type"] == "json_object"
    assert "refactor the queue port" in captured["body"]["messages"][1]["content"]


def test_openai_decider_rejects_non_json_content(monkeypatch) -> None:
    _patch_urlopen(
        monkeypatch, json.dumps({"choices": [{"message": {"content": "sure!"}}]}).encode()
    )
    decider = OpenAICompatibleDecider(api_key="k", model="m", base_url="https://x/v1")

    with pytest.raises(DecisionError):
        decider.decide("state", QUESTIONS)


def test_openai_decider_rejects_a_missing_question(monkeypatch) -> None:
    partial = {"choices": [{"message": {"content": json.dumps({"route": {"choice": "local"}})}}]}
    _patch_urlopen(monkeypatch, json.dumps(partial).encode())
    decider = OpenAICompatibleDecider(api_key="k", model="m", base_url="https://x/v1")

    with pytest.raises(DecisionError):
        decider.decide("state", QUESTIONS)


def test_openai_decider_rejects_an_out_of_set_choice(monkeypatch) -> None:
    bad = {"choices": [{"message": {"content": json.dumps({"route": {"choice": "banana"}})}}]}
    _patch_urlopen(monkeypatch, json.dumps(bad).encode())
    decider = OpenAICompatibleDecider(api_key="k", model="m", base_url="https://x/v1")

    with pytest.raises(DecisionError):
        decider.decide("state", {"route": QUESTIONS["route"]})


def test_openai_decider_surfaces_http_errors(monkeypatch) -> None:
    import urllib.error

    def boom(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 429, "rate limited", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    decider = OpenAICompatibleDecider(api_key="k", model="m", base_url="https://x/v1")

    with pytest.raises(DecisionError):
        decider.decide("state", QUESTIONS)

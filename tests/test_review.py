from __future__ import annotations

import pytest

from dear_agent.review import (
    ModelReviewer,
    NoReviewer,
    ReviewError,
    build_reviewer,
)


def test_no_reviewer_approves() -> None:
    assert NoReviewer().review(diff="x", instructions="y").approved


def test_model_reviewer_parses_approve() -> None:
    def chat(**kwargs: object) -> str:
        return '{"verdict": "approve", "summary": "looks good", "notes": ""}'

    verdict = ModelReviewer(api_key="k", model="m", _chat=chat).review(
        diff="diff", instructions="do it"
    )

    assert verdict.approved
    assert verdict.summary == "looks good"


def test_model_reviewer_parses_revise_and_strips_a_code_fence() -> None:
    def chat(**kwargs: object) -> str:
        return '```json\n{"verdict": "revise", "summary": "needs work", "notes": "add a test"}\n```'

    verdict = ModelReviewer(api_key="k", model="m", _chat=chat).review(
        diff="diff", instructions="do it"
    )

    assert not verdict.approved
    assert verdict.notes == "add a test"


def test_model_reviewer_rejects_an_unknown_verdict() -> None:
    def chat(**kwargs: object) -> str:
        return '{"verdict": "maybe"}'

    with pytest.raises(ReviewError, match="approve|revise"):
        ModelReviewer(api_key="k", model="m", _chat=chat).review(diff="d", instructions="i")


def test_build_reviewer_is_none_when_disabled() -> None:
    assert build_reviewer({}) is None
    assert build_reviewer({"DEAR_AGENT_REVIEWER": "none"}) is None


def test_build_reviewer_builds_an_openai_compatible_model() -> None:
    reviewer = build_reviewer(
        {
            "DEAR_AGENT_REVIEWER": "openai-compat",
            "DEAR_AGENT_REVIEWER_BASE_URL": "http://127.0.0.1:8080/v1",
            "DEAR_AGENT_REVIEWER_MODEL": "qwen",
        }
    )

    assert isinstance(reviewer, ModelReviewer)
    assert reviewer.model == "qwen"


def test_build_reviewer_requires_a_model() -> None:
    with pytest.raises(ReviewError, match="MODEL"):
        build_reviewer({"DEAR_AGENT_REVIEWER": "openai-compat"})


def test_build_reviewer_rejects_an_unknown_choice() -> None:
    with pytest.raises(ReviewError, match="unknown DEAR_AGENT_REVIEWER"):
        build_reviewer({"DEAR_AGENT_REVIEWER": "debate-until-green"})

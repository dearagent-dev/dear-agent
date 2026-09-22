from __future__ import annotations

import json

import pytest

from herald.decision.openrouter import OpenRouterCatalog, OpenRouterProvider, parse_catalog
from herald.decision.port import DecisionError, ModelCatalog, ModelInfo
from herald.decision.selection import SelectionPolicy

PAYLOAD = {
    "data": [
        {
            "id": "expensive/big",
            "context_length": 200000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "architecture": {"output_modalities": ["text"]},
            "supported_parameters": ["response_format", "tools"],
        },
        {
            "id": "free/good:free",
            "context_length": 262144,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"output_modalities": ["text"]},
            "supported_parameters": ["response_format", "tools"],
        },
        {
            "id": "free/no-json:free",
            "context_length": 262144,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"output_modalities": ["text"]},
            "supported_parameters": ["tools"],
        },
        {
            "id": "free/tiny:free",
            "context_length": 1024,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"output_modalities": ["text"]},
            "supported_parameters": ["response_format"],
        },
        {
            "id": "free/image:free",
            "context_length": 262144,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"output_modalities": ["image"]},
            "supported_parameters": ["response_format"],
        },
    ]
}


def test_parse_catalog_is_vendor_neutral() -> None:
    models = {m.id: m for m in parse_catalog(PAYLOAD)}

    assert isinstance(models["free/good:free"], ModelInfo)
    assert models["free/good:free"].is_free is True
    assert models["expensive/big"].is_free is False
    assert models["expensive/big"].prompt_price == 0.000003
    assert models["free/good:free"].supports_json is True
    assert models["free/no-json:free"].supports_json is False


def test_selection_policy_ranks_free_first_and_filters() -> None:
    policy = SelectionPolicy(min_context=8192)
    ranked = policy.rank(parse_catalog(PAYLOAD))
    ids = [m.id for m in ranked]

    assert ids[0] == "free/good:free"
    assert "free/no-json:free" not in ids
    assert "free/tiny:free" not in ids
    assert "free/image:free" not in ids


def test_selection_policy_can_forbid_paid() -> None:
    agent = SelectionPolicy(min_context=8192, allow_paid=False)
    ranked = agent.rank(parse_catalog(PAYLOAD))

    assert ranked and all(m.is_free for m in ranked)


def test_selection_policy_uses_paid_when_required() -> None:
    policy = SelectionPolicy(min_context=8192)
    paid_only = [m for m in parse_catalog(PAYLOAD) if not m.is_free]

    assert policy.rank(paid_only)[0].id == "expensive/big"


def test_selection_policy_raises_when_nothing_qualifies() -> None:
    class EmptyCatalog:
        def list_models(self) -> list[ModelInfo]:
            return []

    with pytest.raises(DecisionError):
        SelectionPolicy().select(EmptyCatalog())


def test_openrouter_catalog_fetches_and_parses(monkeypatch) -> None:
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(PAYLOAD).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())

    models = OpenRouterCatalog(api_key="k").list_models()

    assert any(m.id == "free/good:free" for m in models)
    assert isinstance(OpenRouterCatalog(), ModelCatalog)


def test_openrouter_catalog_surfaces_http_errors(monkeypatch) -> None:
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 429, "slow down", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)

    with pytest.raises(DecisionError):
        OpenRouterCatalog(api_key="k").list_models()


def test_openrouter_provider_builds_a_decider_for_a_model() -> None:
    provider = OpenRouterProvider(api_key="k")
    decider = provider.decider_for(ModelInfo(id="free/good:free", supports_json=True))

    assert decider.model == "free/good:free"
    assert decider.base_url == "https://openrouter.ai/api/v1"


def test_selection_on_a_custom_catalog() -> None:
    class StaticCatalog:
        def list_models(self) -> list[ModelInfo]:
            return [ModelInfo(id="local/qwen", context_length=32768, supports_json=True)]

    model = SelectionPolicy().select(StaticCatalog())

    assert model.id == "local/qwen"

from __future__ import annotations

import pytest

from dear_agent.decision.factory import EnvDeciderCatalog, EnvDeciderProvider
from dear_agent.decision.jev import JevDecider
from dear_agent.decision.openai import OpenAICompatibleDecider
from dear_agent.decision.policy import DeciderPolicy
from dear_agent.decision.port import DeciderInfo, DecisionError


class StaticCatalog:
    def __init__(self, infos: list[DeciderInfo]) -> None:
        self._infos = infos

    def list_deciders(self) -> list[DeciderInfo]:
        return list(self._infos)


def test_policy_prefers_native_over_emulated() -> None:
    catalog = StaticCatalog(
        [
            DeciderInfo(id="emu", protocol="openai-compat", native_types=False),
            DeciderInfo(id="native", protocol="system-one", native_types=True),
        ]
    )

    assert DeciderPolicy().select(catalog).id == "native"


def test_policy_prefers_local_over_hosted_at_equal_nativeness() -> None:
    catalog = StaticCatalog(
        [
            DeciderInfo(id="hosted", protocol="system-one", local=False),
            DeciderInfo(id="local", protocol="system-one", local=True),
        ]
    )

    assert DeciderPolicy().select(catalog).id == "local"


def test_policy_honors_the_preferred_id() -> None:
    catalog = StaticCatalog(
        [
            DeciderInfo(id="native", protocol="system-one"),
            DeciderInfo(id="emu", protocol="openai-compat", native_types=False),
        ]
    )

    assert DeciderPolicy(preferred="emu").select(catalog).id == "emu"


def test_policy_require_native_rejects_emulation() -> None:
    catalog = StaticCatalog([DeciderInfo(id="emu", protocol="openai-compat", native_types=False)])

    with pytest.raises(DecisionError):
        DeciderPolicy(require_native=True).select(catalog)


def test_policy_raises_when_nothing_qualifies() -> None:
    with pytest.raises(DecisionError):
        DeciderPolicy().select(StaticCatalog([]))


def test_env_catalog_always_offers_rules() -> None:
    infos = EnvDeciderCatalog(env={}).list_deciders()

    assert [info.id for info in infos] == ["rules"]
    assert infos[0].protocol == "rules"
    assert infos[0].is_native is False


def test_env_catalog_lists_a_native_decider_when_keyed() -> None:
    catalog = EnvDeciderCatalog(env={"TYPESAFE_API_KEY": "k"})

    jev = next(info for info in catalog.list_deciders() if info.id == "jev")

    assert jev.protocol == "system-one"
    assert jev.native_types is True
    assert jev.api_key_env == "TYPESAFE_API_KEY"


def test_env_catalog_marks_a_local_endpoint() -> None:
    catalog = EnvDeciderCatalog(
        env={
            "DEAR_AGENT_DECIDER_BASE_URL": "http://127.0.0.1:8080/v1",
            "DEAR_AGENT_DECIDER_MODEL": "qwen",
        }
    )

    emu = next(info for info in catalog.list_deciders() if info.id == "openai-compat")

    assert emu.protocol == "openai-compat"
    assert emu.native_types is False
    assert emu.local is True


def test_env_provider_builds_a_native_decider() -> None:
    info = DeciderInfo(
        id="jev",
        protocol="system-one",
        endpoint="https://api.typesafe.ai/v1/systemone",
        model="jev-latest",
        api_key_env="TYPESAFE_API_KEY",
    )

    decider = EnvDeciderProvider(env={"TYPESAFE_API_KEY": "k"}).decider_for(info)

    assert isinstance(decider, JevDecider)
    assert decider.model == "jev-latest"


def test_env_provider_builds_an_emulated_decider() -> None:
    info = DeciderInfo(
        id="openai-compat",
        protocol="openai-compat",
        endpoint="http://127.0.0.1:8080/v1",
        model="qwen",
        native_types=False,
    )

    decider = EnvDeciderProvider(env={}).decider_for(info)

    assert isinstance(decider, OpenAICompatibleDecider)
    assert decider.base_url == "http://127.0.0.1:8080/v1"


def test_env_provider_requires_the_configured_key() -> None:
    info = DeciderInfo(id="jev", protocol="system-one", api_key_env="TYPESAFE_API_KEY")

    with pytest.raises(DecisionError):
        EnvDeciderProvider(env={}).decider_for(info)


def test_env_provider_requires_a_model_for_emulation() -> None:
    info = DeciderInfo(
        id="openai-compat",
        protocol="openai-compat",
        endpoint="http://127.0.0.1:8080/v1",
        model="",
        native_types=False,
    )

    with pytest.raises(DecisionError):
        EnvDeciderProvider(env={}).decider_for(info)

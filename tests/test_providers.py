from __future__ import annotations

import pytest

from dear_agent.providers.registry import (
    Provider,
    ProviderError,
    ProviderKind,
    ProviderRegistry,
    UnknownProviderError,
)


def registry() -> ProviderRegistry:
    reg = ProviderRegistry()
    reg.register(
        Provider(
            id="anthropic",
            base_url="https://api.anthropic.com",
            model="claude",
            api_key_env="ANTHROPIC_API_KEY",
        )
    )
    reg.register(
        Provider(
            id="local",
            base_url="http://127.0.0.1:8000/v1",
            model="deepseek",
            api_key_env="DEAR_AGENT_LOCAL_KEY",
        )
    )
    return reg


def test_task_hint_wins() -> None:
    reg = registry()
    reg.set_global_default("anthropic")

    resolved = reg.resolve(task_hint="local")

    assert resolved.id == "local"


def test_project_default_beats_global() -> None:
    reg = registry()
    reg.set_global_default("anthropic")
    reg.set_project_default("dear-agent", "local")

    assert reg.resolve(project="dear-agent").id == "local"


def test_global_default_is_the_fallback() -> None:
    reg = registry()
    reg.set_global_default("anthropic")

    assert reg.resolve().id == "anthropic"


def test_task_hint_may_select_a_model() -> None:
    reg = registry()
    reg.set_global_default("anthropic")

    resolved = reg.resolve(task_hint="anthropic:claude-opus")

    assert resolved.id == "anthropic"
    assert resolved.model == "claude-opus"


def test_unknown_hint_raises() -> None:
    reg = registry()

    with pytest.raises(UnknownProviderError):
        reg.resolve(task_hint="nope")


def test_no_default_and_no_hint_raises() -> None:
    with pytest.raises(ProviderError):
        registry().resolve()


def test_api_key_is_a_name_not_a_value() -> None:
    provider = registry().providers["anthropic"]

    assert provider.api_key_env == "ANTHROPIC_API_KEY"
    assert "sk-" not in provider.api_key_env


def test_local_provider_is_detected() -> None:
    reg = registry()

    assert reg.providers["local"].is_local() is True
    assert reg.providers["anthropic"].is_local() is False


def test_registry_round_trips_through_a_declarative_mapping() -> None:
    reg = ProviderRegistry()
    reg.register(
        Provider(id="local", kind=ProviderKind.OPENAI_COMPATIBLE, base_url="http://localhost:9/v1")
    )

    assert reg.providers["local"].kind is ProviderKind.OPENAI_COMPATIBLE

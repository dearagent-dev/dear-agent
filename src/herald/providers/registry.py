from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ProviderError(Exception):
    """Base class for provider selection errors."""


class UnknownProviderError(ProviderError):
    """The requested provider id is not registered."""


class ProviderKind(StrEnum):
    OPENAI_COMPATIBLE = "openai-compatible"
    NATIVE = "native"


@dataclass(slots=True, frozen=True)
class Provider:
    """A model backend, declarative and secret-free.

    ``api_key_env`` is the **name** of the environment variable holding the key, never the
    key itself, so a provider definition is safe to commit.
    """

    id: str
    kind: ProviderKind = ProviderKind.OPENAI_COMPATIBLE
    base_url: str = ""
    model: str = ""
    api_key_env: str | None = None

    def is_local(self) -> bool:
        return self.base_url.startswith("http://127.0.0.1") or self.base_url.startswith(
            "http://localhost"
        )


@dataclass(slots=True)
class ProviderRegistry:
    """Resolves which provider a task should use.

    Precedence follows ``docs/providers.md``: task hint, then project default, then global
    default. The task hint may be ``provider`` or ``provider:model``.
    """

    providers: dict[str, Provider] = field(default_factory=dict)
    global_default: str | None = None
    project_defaults: dict[str, str] = field(default_factory=dict)

    def register(self, provider: Provider) -> None:
        self.providers[provider.id] = provider

    def set_global_default(self, provider_id: str) -> None:
        self._require(provider_id)
        self.global_default = provider_id

    def set_project_default(self, project: str, provider_id: str) -> None:
        self._require(provider_id)
        self.project_defaults[project] = provider_id

    def resolve(self, *, task_hint: str | None = None, project: str | None = None) -> Provider:
        hint_id, hint_model = _split_hint(task_hint)
        if hint_id is not None:
            provider = self._require(hint_id)
            return _with_model(provider, hint_model)

        if project is not None and project in self.project_defaults:
            return self._require(self.project_defaults[project])

        if self.global_default is not None:
            return self._require(self.global_default)

        raise ProviderError("no provider could be resolved")

    def _require(self, provider_id: str) -> Provider:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise UnknownProviderError(provider_id)
        return provider


def _split_hint(hint: str | None) -> tuple[str | None, str | None]:
    if not hint:
        return None, None
    if ":" in hint:
        provider_id, _, model = hint.partition(":")
        return provider_id or None, model or None
    return hint, None


def _with_model(provider: Provider, model: str | None) -> Provider:
    if model is None or model == provider.model:
        return provider
    return Provider(
        id=provider.id,
        kind=provider.kind,
        base_url=provider.base_url,
        model=model,
        api_key_env=provider.api_key_env,
    )


__all__ = [
    "Provider",
    "ProviderError",
    "ProviderKind",
    "ProviderRegistry",
    "UnknownProviderError",
]

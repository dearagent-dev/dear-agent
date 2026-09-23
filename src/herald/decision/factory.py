from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

from herald.decision.log import DecisionLog, DecisionRecord, DecisionStore
from herald.decision.policy import DeciderPolicy
from herald.decision.port import (
    Answer,
    Decider,
    DeciderInfo,
    Decision,
    DecisionError,
    DecisionKind,
    Question,
)
from herald.decision.rules import RuleDecider

DEFAULT_CONFIDENCE_THRESHOLD = 0.5
DEFAULT_OPENAI_BASE_URL = "https://openrouter.ai/api/v1"

_RULES = "rules"
_SYSTEM_ONE = "system-one"
_OPENAI_COMPAT = "openai-compat"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}
# ``openrouter`` was the value before the plane was renamed; keep it working as an alias.
_ALIASES = {"openrouter": _OPENAI_COMPAT}


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
    log: DecisionStore

    def decide(self, state: str, questions: dict[str, Question]) -> Decision:
        decision = self.decider.decide(state, questions)
        with contextlib.suppress(OSError):
            self.log.record(DecisionRecord.from_decision(state, questions, decision))
        return decision


@dataclass(slots=True)
class EnvDeciderCatalog:
    """The deciders this deployment can run, read from ``HERALD_DECIDER_*`` (ADR 0004, M7).

    The environment-backed :class:`~herald.decision.port.DeciderCatalog`: the core asks it
    *what exists* and never learns the answer came from variables. The deterministic ``rules``
    fallback is always listed; a native System One entry appears when its provider is
    configured, and the emulated OpenAI-compatible entry appears when a base URL or key is
    set. Entries reference credentials by ``api_key_env`` and never carry a secret.
    """

    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    def list_deciders(self) -> list[DeciderInfo]:
        infos = [DeciderInfo(id=_RULES, protocol=_RULES, native_types=False, local=True, free=True)]
        system_one = self._system_one()
        if system_one is not None:
            infos.append(system_one)
        emulated = self._openai_compat()
        if emulated is not None:
            infos.append(emulated)
        return infos

    def _system_one(self) -> DeciderInfo | None:
        if not self.env.get("TYPESAFE_API_KEY"):
            return None
        from herald.decision.jev import DEFAULT_ENDPOINT, DEFAULT_MODEL

        return DeciderInfo(
            id="jev",
            protocol=_SYSTEM_ONE,
            endpoint=self.env.get("HERALD_DECIDER_ENDPOINT", DEFAULT_ENDPOINT),
            model=self.env.get("HERALD_DECIDER_MODEL", DEFAULT_MODEL),
            native_types=True,
            api_key_env="TYPESAFE_API_KEY",
        )

    def _openai_compat(self) -> DeciderInfo | None:
        endpoint = self.env.get("HERALD_DECIDER_BASE_URL")
        api_key_env = "OPENROUTER_API_KEY" if self.env.get("OPENROUTER_API_KEY") else None
        if endpoint is None and api_key_env is None:
            return None
        endpoint = endpoint or DEFAULT_OPENAI_BASE_URL
        return DeciderInfo(
            id=_OPENAI_COMPAT,
            protocol=_OPENAI_COMPAT,
            endpoint=endpoint,
            model=self.env.get("HERALD_DECIDER_MODEL", ""),
            native_types=False,
            local=_is_local(endpoint),
            api_key_env=api_key_env,
        )


@dataclass(slots=True)
class EnvDeciderProvider:
    """Builds the concrete decider for a :class:`DeciderInfo` from the environment.

    One provider for the in-tree protocols: ``rules`` is local, ``system-one`` is
    :class:`~herald.decision.jev.JevDecider`, and ``openai-compat`` is the emulated
    :class:`~herald.decision.openai.OpenAICompatibleDecider`. Credentials are resolved here,
    by ``api_key_env``, so a catalog entry never holds a secret.
    """

    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    def decider_for(self, info: DeciderInfo) -> Decider:
        if info.protocol == _RULES:
            return RuleDecider()
        if info.protocol == _SYSTEM_ONE:
            return self._system_one(info)
        if info.protocol == _OPENAI_COMPAT:
            return self._openai_compat(info)
        raise DecisionError(f"unsupported decider protocol {info.protocol!r}")

    def _system_one(self, info: DeciderInfo) -> Decider:
        from herald.decision.jev import DEFAULT_ENDPOINT, DEFAULT_MODEL, JevDecider

        return JevDecider(
            api_key=self._key(info),
            model=info.model or DEFAULT_MODEL,
            endpoint=info.endpoint or DEFAULT_ENDPOINT,
        )

    def _openai_compat(self, info: DeciderInfo) -> Decider:
        from herald.decision.openai import OpenAICompatibleDecider

        if not info.model:
            raise DecisionError("HERALD_DECIDER_MODEL is required for an openai-compat decider")
        return OpenAICompatibleDecider(
            api_key=self._key(info) or "local",
            model=info.model,
            base_url=info.endpoint or DEFAULT_OPENAI_BASE_URL,
            extra_headers=self._referer_headers(),
        )

    def _key(self, info: DeciderInfo) -> str:
        if info.api_key_env is None:
            return ""
        value = self.env.get(info.api_key_env)
        if not value:
            raise DecisionError(f"{info.api_key_env} is required for decider {info.id!r}")
        return value

    def _referer_headers(self) -> dict[str, str]:
        referer = self.env.get("HERALD_DECIDER_REFERER")
        if not referer:
            return {}
        return {"HTTP-Referer": referer, "X-Title": "Herald"}


def build_decider() -> Decider | None:
    """Build the configured decider, or ``None`` when disabled (ADR 0004).

    ``HERALD_DECIDER`` names the decider to use (``rules`` is the default): the catalog lists
    what this deployment can run, the policy applies native > emulated and local > hosted, and
    the provider builds it. A model-backed decider is wrapped in :class:`FallbackDecider` so
    any error or low confidence falls back to rules; every decider is wrapped in
    :class:`LoggingDecider` when ``HERALD_DECIDER_LOG`` is set.
    """
    requested = _normalize(os.environ.get("HERALD_DECIDER", _RULES))
    if requested == "none":
        return None

    threshold = float(os.environ.get("HERALD_DECIDER_THRESHOLD", DEFAULT_CONFIDENCE_THRESHOLD))
    require_native = os.environ.get("HERALD_DECIDER_REQUIRE_NATIVE", "").lower() == "true"

    catalog = EnvDeciderCatalog()
    if requested not in {info.id for info in catalog.list_deciders()}:
        raise DecisionError(f"decider {requested!r} is not configured")

    policy = DeciderPolicy(preferred=requested, require_native=require_native)
    info = policy.select(catalog)
    decider = EnvDeciderProvider().decider_for(info)
    if info.protocol != _RULES:
        decider = FallbackDecider(primary=decider, fallback=RuleDecider(), threshold=threshold)
    return _maybe_log(decider)


def _normalize(name: str) -> str:
    name = name.strip().lower()
    return _ALIASES.get(name, name)


def _is_local(endpoint: str) -> bool:
    return (urlparse(endpoint).hostname or "") in _LOCAL_HOSTS


def _maybe_log(decider: Decider) -> Decider:
    target = os.environ.get("HERALD_DECIDER_LOG")
    if not target:
        return decider
    if target == "postgres":
        from herald.db import connect, init_schema
        from herald.decision.log import PostgresDecisionLog

        dsn = os.environ.get("HERALD_DATABASE_URL")
        if not dsn:
            raise DecisionError("HERALD_DATABASE_URL is required for HERALD_DECIDER_LOG=postgres")
        conn = connect(dsn)
        init_schema(conn)
        return LoggingDecider(decider=decider, log=PostgresDecisionLog(conn))
    from pathlib import Path

    return LoggingDecider(decider=decider, log=DecisionLog(path=Path(target)))


__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_OPENAI_BASE_URL",
    "EnvDeciderCatalog",
    "EnvDeciderProvider",
    "FallbackDecider",
    "LoggingDecider",
    "build_decider",
]

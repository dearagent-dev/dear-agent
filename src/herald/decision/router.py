from __future__ import annotations

from dataclasses import dataclass

from herald.decision.port import Decider, DecisionKind, Question
from herald.providers.registry import Provider, ProviderRegistry

# The questions the router asks; kept here so the rule fallback and model deciders agree.
QUESTION_MODEL = "model"
QUESTION_NEEDS_HUMAN = "needs_human"

ROUTE_QUESTION = Question(
    kind=DecisionKind.CHOICE,
    instructions="Which model class should execute this repository task?",
    criteria={
        "local": "small, mechanical, low-risk change a small local model can complete correctly",
        "hosted": "needs real reasoning: architecture, security, subtle debugging, design",
    },
)

HUMAN_QUESTION = Question(
    kind=DecisionKind.NOUL,
    instructions=(
        "Would a maintainer want to approve this before an agent runs it, because it touches "
        "production, deployment, data, or is otherwise high-stakes?"
    ),
)


@dataclass(slots=True, frozen=True)
class Routing:
    """The dispatch plan for a task: which provider, and whether a human must approve."""

    provider: Provider
    needs_human: bool
    reason: str


@dataclass(slots=True)
class ModelRouter:
    """Chooses a provider for a task from a typed decision (ADR 0004, M7.2).

    The decision model answers two questions — model class and whether a human should look —
    and the router maps the class to a registered provider. It is advisory: an explicit task
    hint still wins, and if the decider is absent or the class is unknown it falls back to
    the registry's static precedence. It picks a model; it never authorizes an action.
    """

    registry: ProviderRegistry
    decider: Decider | None = None
    local_provider_id: str | None = None
    hosted_provider_id: str | None = None

    def route(
        self,
        *,
        state: str,
        task_hint: str | None = None,
        project: str | None = None,
    ) -> Routing:
        if task_hint:
            return Routing(
                provider=self.registry.resolve(task_hint=task_hint, project=project),
                needs_human=False,
                reason="explicit task hint",
            )

        if self.decider is None:
            return Routing(
                provider=self.registry.resolve(project=project),
                needs_human=False,
                reason="no decider; static precedence",
            )

        decision = self.decider.decide(
            state, {QUESTION_MODEL: ROUTE_QUESTION, QUESTION_NEEDS_HUMAN: HUMAN_QUESTION}
        )
        chosen = decision.choice(QUESTION_MODEL)
        needs_human = (decision.noul(QUESTION_NEEDS_HUMAN) or 0.0) >= 0.5
        provider_id = self._provider_for(chosen)
        if provider_id is None:
            return Routing(
                provider=self.registry.resolve(project=project),
                needs_human=needs_human,
                reason=f"decider chose {chosen!r} but no provider is mapped; static precedence",
            )
        return Routing(
            provider=self.registry.resolve(task_hint=provider_id, project=project),
            needs_human=needs_human,
            reason=f"decider chose {chosen!r}",
        )

    def _provider_for(self, chosen: str | None) -> str | None:
        if chosen == "local":
            return self.local_provider_id
        if chosen == "hosted":
            return self.hosted_provider_id
        return None


__all__ = [
    "HUMAN_QUESTION",
    "QUESTION_MODEL",
    "QUESTION_NEEDS_HUMAN",
    "ROUTE_QUESTION",
    "ModelRouter",
    "Routing",
]

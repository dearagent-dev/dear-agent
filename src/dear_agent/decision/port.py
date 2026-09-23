from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class DecisionError(Exception):
    """A decision could not be obtained from the backing model."""


class DecisionKind(StrEnum):
    """The three System One question shapes (ADR 0004)."""

    CHOICE = "choice"
    SCORE = "score"
    NOUL = "noul"


@dataclass(slots=True, frozen=True)
class Question:
    """One typed question about a state.

    ``criteria`` is the label set for a ``choice`` (key -> meaning) or the ordered rubric for
    a ``score`` (low -> high). A ``noul`` needs only ``instructions``.
    """

    kind: DecisionKind
    instructions: str
    criteria: dict[str, str] | list[str] | None = None


@dataclass(slots=True, frozen=True)
class Answer:
    """A typed answer with its confidence.

    Exactly one of ``choice``/``score``/``noul`` is set, matching ``kind``. ``confidence`` is
    the model's own certainty (0-1); callers branch on it and fall back below their earned
    threshold. It is not a probability that the answer is correct.
    """

    kind: DecisionKind
    choice: str | None = None
    score: float | None = None
    noul: float | None = None
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class Decision:
    """The answers to a request, keyed by question id."""

    answers: dict[str, Answer] = field(default_factory=dict)

    def choice(self, question_id: str) -> str | None:
        answer = self.answers.get(question_id)
        return answer.choice if answer else None

    def score(self, question_id: str) -> float | None:
        answer = self.answers.get(question_id)
        return answer.score if answer else None

    def noul(self, question_id: str) -> float | None:
        answer = self.answers.get(question_id)
        return answer.noul if answer else None

    def confidence(self, question_id: str) -> float | None:
        answer = self.answers.get(question_id)
        return answer.confidence if answer else None


@runtime_checkable
class Decider(Protocol):
    """A decision model: state + typed questions in, typed answers out (ADR 0004).

    Implementations must be advisory: a caller may branch on a decision, but must never
    treat it as a security boundary, and must fall back to a deterministic rule when the
    decider is absent, unreachable, or unconfident.
    """

    def decide(self, state: str, questions: dict[str, Question]) -> Decision:
        """Return typed answers for ``state``. Raise :class:`DecisionError` on failure."""
        ...


@dataclass(slots=True, frozen=True)
class DeciderInfo:
    """Metadata describing one decider a catalog offers (ADR 0004, M7).

    Vendor-neutral: the core reads this and never imports a vendor SDK. ``protocol`` says
    *how* the decider answers: ``system-one`` is native (choice/score/noul in one typed
    pass — Jev, Laya), ``openai-compat`` emulates it over a chat endpoint (``native_types``
    is then false and answers are parsed from text), and ``rules`` is the deterministic
    fallback. ``native_types`` decides preference: a native decider is preferred over an
    emulated one, because it cannot return a malformed type.
    """

    id: str
    protocol: str = "system-one"
    endpoint: str = ""
    model: str = ""
    native_types: bool = True
    local: bool = False
    free: bool = False
    api_key_env: str | None = None

    @property
    def is_native(self) -> bool:
        return self.protocol == "system-one" and self.native_types


@runtime_checkable
class DeciderCatalog(Protocol):
    """Lists the deciders available to this deployment, with the metadata to choose among them.

    The core asks the catalog and applies its own
    :class:`~dear_agent.decision.policy.DeciderPolicy`; it never knows whether the entries come
    from the environment, TypeSafe, or a local list.
    """

    def list_deciders(self) -> list[DeciderInfo]:
        """Return the configured deciders. Raise :class:`DecisionError` on failure."""
        ...


@runtime_checkable
class DeciderProvider(Protocol):
    """Builds a :class:`Decider` for a :class:`DeciderInfo` chosen from a catalog.

    The catalog decides *what exists*; the provider decides *how to run one of them* (which
    adapter, with what credentials). One implementation per protocol keeps the core free of
    vendor knowledge.
    """

    def decider_for(self, info: DeciderInfo) -> Decider:
        """Return a decider for ``info``. Raise :class:`DecisionError` if unsupported."""
        ...


__all__ = [
    "Answer",
    "Decider",
    "DeciderCatalog",
    "DeciderInfo",
    "DeciderProvider",
    "Decision",
    "DecisionError",
    "DecisionKind",
    "Question",
]

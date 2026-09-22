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
class ModelInfo:
    """A model offered by a catalog: identity, limits, price and capabilities.

    Vendor-neutral: no provider name, URL or wire format leaks in. ``prompt_price`` and
    ``completion_price`` are per-token costs in the catalog's own units; a catalog documents
    its convention, and ``is_free`` is true when both are zero.
    """

    id: str
    context_length: int = 0
    prompt_price: float = 0.0
    completion_price: float = 0.0
    supports_json: bool = False
    output_modalities: tuple[str, ...] = ("text",)

    @property
    def is_free(self) -> bool:
        return self.prompt_price == 0.0 and self.completion_price == 0.0

    def supports(self, *, min_context: int) -> bool:
        """True when this model meets a decider's requirements.

        A decider needs a **text-only** output (a model that also emits audio or images is not
        a chat model), JSON support, and enough context.
        """
        return (
            self.supports_json
            and self.output_modalities == ("text",)
            and (self.context_length >= min_context)
        )


@runtime_checkable
class ModelCatalog(Protocol):
    """Lists the models a provider offers, with the metadata to choose among them.

    The core asks the catalog for models and applies its own selection policy; it never
    knows whether the models come from OpenRouter, a local vLLM, or a static list. This is
    what lets Herald use a hosted catalog today and a self-hosted one tomorrow by swapping
    the implementation (golden rule 5).
    """

    def list_models(self) -> list[ModelInfo]:
        """Return the available models. Raise :class:`DecisionError` on failure."""
        ...


@runtime_checkable
class ModelProvider(Protocol):
    """Builds a :class:`Decider` for a specific model chosen from a catalog (ADR 0004).

    The catalog decides *what exists*; the provider decides *how to run one of them*. Keeping
    them apart means the selection policy is written once against the core, and a provider
    (OpenRouter, a local endpoint, TypeSafe) is one implementation of both ports.
    """

    def decider_for(self, model: ModelInfo) -> Decider:
        """Return a decider bound to ``model``. Raise :class:`DecisionError` if unsupported."""
        ...


__all__ = [
    "Answer",
    "Decider",
    "Decision",
    "DecisionError",
    "DecisionKind",
    "ModelCatalog",
    "ModelInfo",
    "ModelProvider",
    "Question",
]

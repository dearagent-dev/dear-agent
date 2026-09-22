from __future__ import annotations

from dataclasses import dataclass

from herald.decision.port import DecisionError, DecisionKind, ModelCatalog, ModelInfo, Question

# The questions the router asks; kept here so rule fallback and model deciders agree.
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


@dataclass(slots=True)
class SelectionPolicy:
    """Chooses the most convenient model from a catalog (ADR 0004, golden rule 5).

    Vendor-neutral: it only sees :class:`ModelInfo`. The order is **free first**, then
    cheapest, then larger context — so Herald can validate on a free tier and, by changing
    nothing but the catalog, switch to paid or self-hosted models.
    """

    min_context: int = 8192
    allow_paid: bool = True

    def select(self, catalog: ModelCatalog) -> ModelInfo:
        candidates = self.rank(catalog.list_models())
        if not candidates:
            raise DecisionError("no catalog model meets the requirements")
        return candidates[0]

    def rank(self, models: list[ModelInfo]) -> list[ModelInfo]:
        usable = [m for m in models if m.supports(min_context=self.min_context)]
        if not self.allow_paid:
            usable = [m for m in usable if m.is_free]
        return sorted(
            usable,
            key=lambda m: (
                not m.is_free,
                m.prompt_price + m.completion_price,
                -m.context_length,
                m.id,
            ),
        )


__all__ = ["QUESTION_MODEL", "QUESTION_NEEDS_HUMAN", "ROUTE_QUESTION", "SelectionPolicy"]

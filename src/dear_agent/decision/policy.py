from __future__ import annotations

from dataclasses import dataclass

from dear_agent.decision.port import DeciderCatalog, DeciderInfo, DecisionError


@dataclass(slots=True)
class DeciderPolicy:
    """Chooses the most appropriate decider from a catalog (ADR 0004).

    Ordering, all in the core and vendor-neutral:

    1. **preferred** id, when the operator named one — it wins outright.
    2. **native** typed deciders before emulated ones (a native System One cannot return a
       malformed type; an OpenAI-compatible one can).
    3. **local** before hosted (golden rule 5: keep judgment on the box when possible).
    4. **free** before paid.
    5. stable by id.

    ``require_native`` forbids emulated deciders entirely (for operators who demand
    type-safety guarantees). If nothing qualifies, it raises and the caller falls back to
    the deterministic :class:`~dear_agent.decision.rules.RuleDecider`.
    """

    preferred: str | None = None
    require_native: bool = False

    def select(self, catalog: DeciderCatalog) -> DeciderInfo:
        candidates = self.rank(catalog.list_deciders())
        if not candidates:
            raise DecisionError("no decider in the catalog meets the requirements")
        return candidates[0]

    def rank(self, infos: list[DeciderInfo]) -> list[DeciderInfo]:
        usable = [i for i in infos if not self.require_native or i.is_native]
        if self.preferred:
            preferred = [i for i in usable if i.id == self.preferred]
            if preferred:
                return preferred + [i for i in usable if i.id != self.preferred]
        return sorted(
            usable,
            key=lambda i: (
                not i.is_native,
                not i.local,
                not i.free,
                i.id,
            ),
        )


__all__ = ["DeciderPolicy"]

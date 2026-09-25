from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from dear_agent.decision.port import Decider, DecisionKind, Question
from dear_agent.queue.models import Task, TaskSpec
from dear_agent.runners.port import Runner
from dear_agent.runners.worktree import RunResult, Worktree
from dear_agent.sandbox import Sandbox

# The 10 "harness" name is part of the routing decision contract, so the factory can map it.
HARNESS_QUESTION = Question(
    kind=DecisionKind.CHOICE,
    instructions="Which coding harness should run this task?",
    criteria={
        "local": "the lightweight/cheap harness for mechanical, low-risk changes",
        "hosted": "the strong harness for architecture, security, subtle debugging, design",
    },
)


@dataclass(slots=True)
class RoutingRunner:
    """Chooses a harness per task and delegates to it (ADR 0004, M7.2).

    Implements the :class:`~dear_agent.runners.port.Runner` port so the executor needs no change:
    before running, it asks the decider which harness fits the task and dispatches to the
    runner registered for that class. This is where "the router picks the model/harness per
    job" becomes real. It is advisory and fail-open: no decider, an unknown class, or a
    missing runner falls back to ``default``.
    """

    runners: dict[str, Runner]
    decider: Decider | None = None
    default: str = "hosted"
    task_field: Callable[[Task, TaskSpec], str] = field(
        default=lambda task, spec: _state(task, spec)
    )
    # The choice is cached per task so ``session_for`` and ``run`` agree even when the decider
    # is not deterministic (a hosted model), and the decider is asked once per run.
    _cached_task: str | None = field(default=None, compare=False, repr=False)
    _cached_choice: str | None = field(default=None, compare=False, repr=False)

    def run(self, task: Task, spec: TaskSpec, worktree: Worktree) -> RunResult:
        runner = self._runner_for(self._choose(task, spec))
        return runner.run(task, spec, worktree)

    def session_for(self, task: Task, spec: TaskSpec) -> Sandbox | None:
        """The session of the harness chosen for this task, so the verify gate shares it."""
        runner = self._runner_for(self._choose(task, spec))
        return getattr(runner, "sandbox", None)

    def _runner_for(self, chosen: str) -> Runner:
        runner = self.runners.get(chosen) or self.runners.get(self.default)
        if runner is None:
            raise KeyError(f"no runner registered for {chosen!r} or default {self.default!r}")
        return runner

    def _choose(self, task: Task, spec: TaskSpec) -> str:
        if self._cached_task == task.id and self._cached_choice is not None:
            return self._cached_choice
        chosen = self._decide(task, spec)
        self._cached_task, self._cached_choice = task.id, chosen
        return chosen

    def _decide(self, task: Task, spec: TaskSpec) -> str:
        if self.decider is None:
            return self.default
        try:
            decision = self.decider.decide(
                self.task_field(task, spec), {"harness": HARNESS_QUESTION}
            )
        except Exception:  # noqa: BLE001 - routing must never break a run
            return self.default
        return decision.choice("harness") or self.default


def _state(task: Task, spec: TaskSpec) -> str:
    parts = [task.subject or "", spec.instructions or ""]
    return "\n".join(part for part in parts if part)


__all__ = ["HARNESS_QUESTION", "RoutingRunner"]

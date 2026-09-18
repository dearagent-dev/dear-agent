from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from herald.idle.proposals import Proposal, ProposalGenerator
from herald.idle.signals import SignalCollector, Signals
from herald.queue.models import TaskState
from herald.queue.port import Queue

DEFAULT_MAX_PER_RUN = 1
DEFAULT_MIN_QUEUE_DEPTH = 1

ProposalSubmitter = Callable[[Proposal], str | None]


@dataclass(slots=True)
class IdleReport:
    """What the idle loop did on one tick."""

    skipped: bool = False
    reason: str = ""
    proposed: list[str] = field(default_factory=list)


@dataclass(slots=True)
class IdleBudget:
    """Caps idle work so it can never starve real, user-requested work."""

    max_per_run: int = DEFAULT_MAX_PER_RUN
    min_queue_depth: int = DEFAULT_MIN_QUEUE_DEPTH

    def allows(self, *, queued: int) -> bool:
        return queued < self.min_queue_depth


class IdleLoop:
    """Proposes work from recent activity when the queue is empty.

    A proposal is handed to a ``submit`` callback that turns it into a task through the
    normal pipeline (for example, the ControlPlane ingesting a self-addressed message).
    The loop never executes a proposal directly and never writes to a protected branch;
    proposals are approval-gated by construction. A budget keeps idle work from competing
    with real work.
    """

    def __init__(
        self,
        *,
        queue: Queue,
        submit: ProposalSubmitter,
        collector: SignalCollector | None = None,
        generator: ProposalGenerator | None = None,
        budget: IdleBudget | None = None,
        repo_name: str | None = None,
    ) -> None:
        self._queue = queue
        self._submit = submit
        self._collector = collector or SignalCollector()
        self._generator = generator or ProposalGenerator()
        self._budget = budget or IdleBudget()
        self._repo_name = repo_name

    def collect(self, repo_path: str) -> Signals:
        return self._collector.collect(repo_path)

    def tick(self, repo_path: str) -> IdleReport:
        queued = len(self._queue.list(TaskState.QUEUED, limit=1000))
        if not self._budget.allows(queued=queued):
            return IdleReport(skipped=True, reason=f"queue depth {queued}")

        signals = self.collect(repo_path)
        proposals = self._generator.generate(signals, repo_name=self._repo_name)
        proposals = proposals[: self._budget.max_per_run]

        proposed: list[str] = []
        for proposal in proposals:
            submitted = self._submit(proposal)
            if submitted is not None:
                proposed.append(submitted)
        return IdleReport(proposed=proposed)


__all__ = ["IdleBudget", "IdleLoop", "IdleReport", "ProposalSubmitter"]

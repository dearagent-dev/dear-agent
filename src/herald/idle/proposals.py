from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from herald.idle.signals import Signal, SignalKind, Signals

MAX_PROPOSALS = 5


@dataclass(slots=True)
class Proposal:
    """A piece of work Herald suggests when the queue is empty.

    A proposal is never executed blindly: it enters the queue like any other task and is
    approval-gated by default. It carries a repo, a summary and instructions, and links to
    the signals that motivated it.
    """

    title: str
    repo_path: Path
    instructions: str
    evidence: list[str]


class ProposalGenerator:
    """Turns recent signals into a small, bounded set of proposals.

    The generator is deterministic so it can be tested and audited. It prefers TODO
    markers (concrete, local) over commit history (broad). It never runs commands and never
    copies untrusted text into an instruction verbatim beyond a short, sanitized line.
    """

    def __init__(self, *, max_proposals: int = MAX_PROPOSALS) -> None:
        self._max_proposals = max_proposals

    def generate(self, signals: Signals, *, repo_name: str | None = None) -> list[Proposal]:
        proposals: list[Proposal] = []
        name = repo_name or signals.repo_path.name

        for signal in signals.of_kind(SignalKind.TODO):
            proposals.append(self._from_todo(signal, signals.repo_path, name))
            if len(proposals) >= self._max_proposals:
                return proposals

        commits = signals.of_kind(SignalKind.COMMIT)
        if commits and len(proposals) < self._max_proposals:
            proposals.append(self._from_commits(commits, signals.repo_path, name))

        return proposals

    @staticmethod
    def _from_todo(signal: Signal, repo_path: Path, name: str) -> Proposal:
        detail = _sanitize(signal.summary)
        location = signal.evidence or "the repository"
        return Proposal(
            title=f"Resolve {detail}" if detail else "Resolve a TODO marker",
            repo_path=repo_path,
            instructions=(
                f"In {name}, address the marker at {location}: {detail}. "
                "Keep the change small and add or update tests."
            ),
            evidence=[f"{location}: {detail}"],
        )

    @staticmethod
    def _from_commits(commits: list[Signal], repo_path: Path, name: str) -> Proposal:
        recent = [signal.summary for signal in commits[:3]]
        evidence = [f"{signal.evidence} {signal.summary}" for signal in commits[:5]]
        return Proposal(
            title=f"Follow up on recent work in {name}",
            repo_path=repo_path,
            instructions=(
                f"Review the recent commits in {name} ({'; '.join(recent)}) and propose one "
                "concrete follow-up: a missing test, a TODO left behind, or a small hardening."
            ),
            evidence=evidence,
        )


def _sanitize(value: str) -> str:
    """Collapse whitespace and cap length so a marker cannot inject multi-line text."""
    collapsed = " ".join(value.split())
    return collapsed[:120]


__all__ = ["MAX_PROPOSALS", "Proposal", "ProposalGenerator"]

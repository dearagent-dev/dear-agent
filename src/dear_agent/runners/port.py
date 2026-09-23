from __future__ import annotations

from typing import Protocol, runtime_checkable

from dear_agent.queue.models import Task, TaskSpec
from dear_agent.runners.worktree import RunResult, Worktree


@runtime_checkable
class Runner(Protocol):
    """Executes a harness for a claimed task inside an isolated worktree.

    A runner adapter must create/enter an isolated worktree, invoke the harness
    non-interactively, capture structured events, and never touch the user's tree. Dear Agent
    wraps a harness; it never embeds one.
    """

    def run(self, task: Task, spec: TaskSpec, worktree: Worktree) -> RunResult:
        """Run the harness and return evidence (exit code, output, branch, commit, PR)."""
        ...

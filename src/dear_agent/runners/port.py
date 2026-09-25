from __future__ import annotations

from typing import Protocol, runtime_checkable

from dear_agent.queue.models import Task, TaskSpec
from dear_agent.runners.worktree import RunResult, Worktree
from dear_agent.sandbox import Sandbox


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


@runtime_checkable
class SessionProvider(Protocol):
    """A runner that can name the environment session it will run a task in.

    The executor hands that same session to the ``verify`` gate (ADR 0008). This matters when the
    runner picks a harness — and therefore an image — per task (routing): a fixed verifier would
    otherwise run in a different environment than the harness.
    """

    def session_for(self, task: Task, spec: TaskSpec) -> Sandbox | None:
        """The session the harness will run in, or ``None`` when there is no session."""
        ...

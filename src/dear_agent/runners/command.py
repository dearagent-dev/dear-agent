from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from dear_agent.queue.models import Task, TaskSpec
from dear_agent.runners.harness import default_execute, text
from dear_agent.runners.worktree import RunResult, Worktree
from dear_agent.sandbox import NoSandbox, Sandbox

DEFAULT_TIMEOUT_SECONDS = 3600

SubprocessRunner = Callable[[list[str], str, int], subprocess.CompletedProcess[str]]


@dataclass(slots=True)
class CommandRunner:
    """A minimal runner that invokes one configured command in the worktree.

    Like a harness adapter it runs inside the configured sandbox/session, so an
    ``DEAR_AGENT_ISOLATION=podman`` run executes the command in the same container as the
    ``verify`` gate (ADR 0008), and it never touches the user's tree. The command must come from
    configuration, never from an inbound message.
    """

    command: list[str]
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    sandbox: Sandbox = field(default_factory=NoSandbox)
    _execute: SubprocessRunner = field(default=default_execute, repr=False)

    def run(self, task: Task, spec: TaskSpec, worktree: Worktree) -> RunResult:
        argv = self.sandbox.wrap(list(self.command), worktree=worktree.path)
        try:
            completed = self._execute(argv, str(worktree.path), self.timeout_seconds)
        except FileNotFoundError:
            binary = self.command[0] if self.command else "command"
            return RunResult(
                exit_code=127,
                stderr=f"harness binary {binary!r} not found",
                branch=worktree.branch,
            )
        except OSError as exc:
            return RunResult(
                exit_code=126,
                stderr=f"cannot execute command: {exc}",
                branch=worktree.branch,
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                exit_code=124,
                stdout=text(exc.stdout),
                stderr=text(exc.stderr),
                branch=worktree.branch,
            )

        return RunResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            branch=worktree.branch,
        )


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "CommandRunner"]

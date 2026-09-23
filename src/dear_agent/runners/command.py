from __future__ import annotations

import subprocess
from dataclasses import dataclass

from dear_agent.queue.models import Task, TaskSpec
from dear_agent.runners.harness import harness_env
from dear_agent.runners.worktree import RunResult, Worktree

DEFAULT_TIMEOUT_SECONDS = 3600


@dataclass(slots=True)
class CommandRunner:
    """A minimal runner that invokes one command in the worktree.

    This is the base a harness adapter builds on: it is deliberately dumb and contains no
    harness knowledge. The command must be provided by configuration, never derived from
    an inbound message.
    """

    command: list[str]
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    def run(self, task: Task, spec: TaskSpec, worktree: Worktree) -> RunResult:
        try:
            completed = subprocess.run(
                self.command,
                cwd=worktree.path,
                env=harness_env(str(worktree.path)),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
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
            output = exc.stdout or ""
            errors = exc.stderr or ""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            if isinstance(errors, bytes):
                errors = errors.decode(errors="replace")
            return RunResult(exit_code=124, stdout=output, stderr=errors, branch=worktree.branch)

        return RunResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            branch=worktree.branch,
        )

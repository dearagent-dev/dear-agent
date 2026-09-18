from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from herald.queue.models import Task, TaskSpec
from herald.runners.worktree import RunResult, Worktree

DEFAULT_TIMEOUT_SECONDS = 3600

SubprocessRunner = Callable[[list[str], str, int], subprocess.CompletedProcess[str]]


def default_execute(argv: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


@dataclass(slots=True)
class HarnessRunner:
    """Shared behaviour for harness adapters.

    Subclasses only describe the harness argv. The prompt is always a separate argv
    element, never routed through a shell, and the harness runs inside the isolated
    worktree. Herald wraps harnesses; it does not embed them.
    """

    binary: str
    model: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    _execute: SubprocessRunner = field(default=default_execute, repr=False)

    def build_argv(self, spec: TaskSpec) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def run(self, task: Task, spec: TaskSpec, worktree: Worktree) -> RunResult:
        try:
            completed = self._execute(
                self.build_argv(spec), str(worktree.path), self.timeout_seconds
            )
        except FileNotFoundError:
            return RunResult(
                exit_code=127,
                stderr=f"harness binary {self.binary!r} not found",
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

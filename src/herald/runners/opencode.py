from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from herald.queue.models import Task, TaskSpec
from herald.runners.worktree import RunResult, Worktree

DEFAULT_BINARY = "opencode"
DEFAULT_TIMEOUT_SECONDS = 3600

SubprocessRunner = Callable[[list[str], str, int], subprocess.CompletedProcess[str]]


def _execute(argv: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@dataclass(slots=True)
class OpenCodeRunner:
    """Runner adapter for the OpenCode harness.

    Herald wraps OpenCode; it does not embed it. The prompt is passed as a separate argv
    element, never through a shell, so untrusted instructions cannot inject commands. The
    binary and model are configuration.
    """

    model: str | None = None
    binary: str = DEFAULT_BINARY
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    _execute: SubprocessRunner = field(default=_execute, repr=False)

    def build_argv(self, spec: TaskSpec) -> list[str]:
        argv = [self.binary, "run"]
        if self.model:
            argv += ["--model", self.model]
        argv += [spec.instructions]
        return argv

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
                stdout=_text(exc.stdout),
                stderr=_text(exc.stderr),
                branch=worktree.branch,
            )

        return RunResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            branch=worktree.branch,
        )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value

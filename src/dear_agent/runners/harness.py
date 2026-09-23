from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from dear_agent.queue.models import Task, TaskSpec
from dear_agent.runners.worktree import RunResult, Worktree
from dear_agent.sandbox import NoSandbox, Sandbox

DEFAULT_TIMEOUT_SECONDS = 3600

SubprocessRunner = Callable[[list[str], str, int], subprocess.CompletedProcess[str]]


# The harness is untrusted, so it must not inherit Dear Agent's environment. Only the variables
# a coding agent needs to run are passed through; anything else (mail token, database DSN,
# git credentials) stays out. Extend the list by name with ``DEAR_AGENT_HARNESS_ENV``.
HARNESS_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TERM",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
)


def harness_env(cwd: str) -> dict[str, str]:
    """The environment for a harness run.

    The harness is untrusted (golden rule 7), so it does **not** inherit Dear Agent's
    environment: only :data:`HARNESS_ENV_ALLOWLIST` plus the names in ``DEAR_AGENT_HARNESS_ENV``
    (comma-separated) are passed. A secret listed there becomes visible to the run, so keep
    it to a model key if a harness needs one.

    ``subprocess.run(cwd=...)`` changes the child's real working directory but leaves the
    ``PWD`` environment variable pointing at Dear Agent's own process directory. Tools such as
    OpenCode resolve the project from ``PWD``, so without this override a run would edit
    Dear Agent's checkout instead of the isolated worktree.
    """
    names = dict.fromkeys([*HARNESS_ENV_ALLOWLIST, *_harness_env_names()])
    env = {name: os.environ[name] for name in names if name in os.environ}
    env["PWD"] = cwd
    return env


def _harness_env_names() -> list[str]:
    value = os.environ.get("DEAR_AGENT_HARNESS_ENV", "")
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


def default_execute(argv: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=harness_env(cwd),
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
    worktree. Dear Agent wraps harnesses; it does not embed them.

    A ``sandbox`` may wrap the argv in an OS-level jail (default-deny network); when none
    is configured the command still runs in the isolated worktree.
    """

    binary: str
    model: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    sandbox: Sandbox = field(default_factory=NoSandbox)
    _execute: SubprocessRunner = field(default=default_execute, repr=False)

    def build_argv(self, spec: TaskSpec) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def run(self, task: Task, spec: TaskSpec, worktree: Worktree) -> RunResult:
        argv = self.sandbox.wrap(self.build_argv(spec), worktree=worktree.path)
        try:
            completed = self._execute(argv, str(worktree.path), self.timeout_seconds)
        except FileNotFoundError:
            return RunResult(
                exit_code=127,
                stderr=f"harness binary {self.binary!r} not found",
                branch=worktree.branch,
            )
        except OSError as exc:
            # e.g. a permission or exec-format error: the command could not run at all.
            return RunResult(
                exit_code=126,
                stderr=f"cannot execute harness {self.binary!r}: {exc}",
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

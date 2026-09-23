from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dear_agent.runners.harness import harness_env
from dear_agent.sandbox import NoSandbox, Sandbox

DEFAULT_TIMEOUT_SECONDS = 900

Executor = Callable[[Sequence[str], str, int], subprocess.CompletedProcess[str]]


def default_execute(
    argv: Sequence[str], cwd: str, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        env=harness_env(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@dataclass(slots=True, frozen=True)
class VerifyResult:
    """The outcome of a task's verify command."""

    ok: bool
    exit_code: int
    output: str = ""
    blocked: bool = False


@dataclass(slots=True)
class CommandVerifier:
    """Runs a task's ``verify`` command in the worktree, gated by an allowlist.

    Golden rule 7: a command derived from an inbound message is attacker-controlled, so it
    runs only when its argv matches an operator-listed entry **exactly** (no prefix or
    extra args) from ``DEAR_AGENT_VERIFY_ALLOW`` (comma/semicolon-separated full commands).
    It is executed as an argv, never through a shell, with a restricted environment
    (``harness_env``) and inside the configured ``sandbox``. An empty allowlist denies
    everything (fail closed).
    """

    allowed: tuple[tuple[str, ...], ...] = ()
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    sandbox: Sandbox = field(default_factory=NoSandbox)
    _execute: Executor = field(default=default_execute, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, sandbox: Sandbox | None = None) -> CommandVerifier:
        allowed: list[tuple[str, ...]] = []
        for part in (env.get("DEAR_AGENT_VERIFY_ALLOW") or "").replace(";", ",").split(","):
            argv = _argv(part)
            if argv:
                allowed.append(argv)
        return cls(allowed=tuple(allowed), sandbox=sandbox or NoSandbox())

    def allows(self, command: str) -> bool:
        argv = _argv(command)
        return argv is not None and argv in self.allowed

    def run(self, command: str, worktree_path: str) -> VerifyResult:
        argv = _argv(command)
        if argv is None or argv not in self.allowed:
            return VerifyResult(
                ok=False, exit_code=126, blocked=True, output="verify command is not allowed"
            )
        wrapped = self.sandbox.wrap(list(argv), worktree=Path(worktree_path))
        try:
            completed = self._execute(wrapped, worktree_path, self.timeout_seconds)
        except FileNotFoundError:
            return VerifyResult(ok=False, exit_code=127, output=f"{argv[0]!r} not found")
        except subprocess.TimeoutExpired:
            return VerifyResult(ok=False, exit_code=124, output="verify timed out")
        except OSError as exc:
            return VerifyResult(ok=False, exit_code=126, output=str(exc))
        output = f"{completed.stdout or ''}{completed.stderr or ''}"
        return VerifyResult(
            ok=completed.returncode == 0,
            exit_code=completed.returncode,
            output=output[-2000:],
        )


def _argv(command: str) -> tuple[str, ...] | None:
    try:
        argv = tuple(shlex.split(command))
    except ValueError:
        return None
    return argv or None


__all__ = ["CommandVerifier", "VerifyResult", "default_execute"]

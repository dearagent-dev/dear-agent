from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from dear_agent.runners.harness import harness_env

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
    runs only when its argv matches the operator's allowlist (``DEAR_AGENT_VERIFY_ALLOW``). It is
    executed as an argv, never through a shell, so shell metacharacters cannot run. An empty
    allowlist denies everything (fail closed).
    """

    allowed: tuple[tuple[str, ...], ...] = ()
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    _execute: Executor = field(default=default_execute, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> CommandVerifier:
        allowed: list[tuple[str, ...]] = []
        for part in (env.get("DEAR_AGENT_VERIFY_ALLOW") or "").replace(";", ",").split(","):
            argv = _argv(part)
            if argv:
                allowed.append(argv)
        return cls(allowed=tuple(allowed))

    def allows(self, command: str) -> bool:
        argv = _argv(command)
        return argv is not None and any(argv[: len(prefix)] == prefix for prefix in self.allowed)

    def run(self, command: str, worktree_path: str) -> VerifyResult:
        argv = _argv(command)
        if argv is None or not self.allows(command):
            return VerifyResult(
                ok=False, exit_code=126, blocked=True, output="verify command is not allowed"
            )
        try:
            completed = self._execute(argv, worktree_path, self.timeout_seconds)
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

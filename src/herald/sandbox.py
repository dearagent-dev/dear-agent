from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


class SandboxError(RuntimeError):
    """A sandbox could not wrap a command."""


@dataclass(slots=True, frozen=True)
class SandboxPolicy:
    """What a run may touch.

    Default-deny on the network: a run gets no egress unless explicitly allowed. The
    worktree is writable; the rest of the host is not. This is the OS-level half of the
    prompt-injection containment in ``docs/security.md``.
    """

    allow_network: bool = False
    readable_paths: tuple[str, ...] = ("/usr", "/bin", "/lib", "/lib64", "/etc")
    writable_paths: tuple[str, ...] = ()
    env_allowlist: tuple[str, ...] = ("PATH", "HOME", "LANG", "TZ")


@runtime_checkable
class Sandbox(Protocol):
    """Wraps a command so it runs under an OS sandbox."""

    def wrap(self, argv: list[str], *, worktree: Path) -> list[str]: ...

    @property
    def available(self) -> bool: ...


@dataclass(slots=True)
class NoSandbox:
    """Runs the command as-is. Used when no OS sandbox is available."""

    @property
    def available(self) -> bool:
        return False

    def wrap(self, argv: list[str], *, worktree: Path) -> list[str]:
        return list(argv)


@dataclass(slots=True)
class BubblewrapSandbox:
    """Wraps a command in ``bwrap``.

    The worktree is the only writable path; the network is unshared unless the policy
    allows it. ``--die-with-parent`` ensures a killed run leaves nothing behind.
    """

    policy: SandboxPolicy = field(default_factory=SandboxPolicy)
    binary: str = "bwrap"

    @property
    def available(self) -> bool:
        import shutil

        return shutil.which(self.binary) is not None

    def wrap(self, argv: list[str], *, worktree: Path) -> list[str]:
        if not self.available:
            raise SandboxError(f"{self.binary!r} is not available")
        return self.build_wrapped_argv(argv, worktree=worktree)

    def build_wrapped_argv(self, argv: list[str], *, worktree: Path) -> list[str]:
        """Build the ``bwrap`` invocation without checking availability."""
        command: list[str] = [
            self.binary,
            "--die-with-parent",
            "--unshare-pid",
            "--unshare-ipc",
            "--new-session",
        ]
        if not self.policy.allow_network:
            command.append("--unshare-net")
        for path in self.policy.readable_paths:
            command += ["--ro-bind-try", path, path]
        for path in self.policy.writable_paths:
            command += ["--bind-try", path, path]
        command += ["--bind", str(worktree), str(worktree)]
        command += ["--chdir", str(worktree)]
        command += ["--clearenv"]
        for name in self.policy.env_allowlist:
            if name in os.environ:
                command += ["--setenv", name, os.environ[name]]
        command += ["--"]
        command += argv
        return command


__all__ = [
    "BubblewrapSandbox",
    "NoSandbox",
    "Sandbox",
    "SandboxError",
    "SandboxPolicy",
]

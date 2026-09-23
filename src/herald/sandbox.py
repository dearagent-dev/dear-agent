from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
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


def sandbox_policy_from_env(env: Mapping[str, str] | None = None) -> SandboxPolicy:
    """Build the sandbox policy from ``HERALD_SANDBOX_*``.

    - ``HERALD_SANDBOX_NETWORK=true`` lets a run reach the model; otherwise egress is denied
      (safest, but a hosted model will not answer).
    - ``HERALD_SANDBOX_READABLE`` adds read-only paths (e.g. a harness installed outside
      ``/usr``); the resolved harness binary's directory is added automatically.
    - ``HERALD_SANDBOX_WRITABLE`` adds writable paths beyond the worktree (avoid).
    - ``HERALD_SANDBOX_ENV`` adds environment variables to pass through — a secret listed
      here becomes visible to the run, so keep it to non-secrets.

    ``$HOME`` is never mounted; credentials in ``~/.ssh`` or ``~/.config`` stay invisible.
    """
    source = env if env is not None else os.environ
    defaults = SandboxPolicy()
    readable = list(defaults.readable_paths) + _split_paths(source.get("HERALD_SANDBOX_READABLE"))
    binary_dir = _harness_dir(source)
    if binary_dir:
        readable.append(binary_dir)
    env_names = list(defaults.env_allowlist) + _split_paths(source.get("HERALD_SANDBOX_ENV"))
    return SandboxPolicy(
        allow_network=source.get("HERALD_SANDBOX_NETWORK", "false").lower() == "true",
        readable_paths=tuple(dict.fromkeys(readable)),
        writable_paths=tuple(_split_paths(source.get("HERALD_SANDBOX_WRITABLE"))),
        env_allowlist=tuple(dict.fromkeys(env_names)),
    )


def _split_paths(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").replace(";", ",").split(",") if part.strip()]


def _harness_dir(env: Mapping[str, str]) -> str | None:
    binary = env.get("HERALD_HARNESS_BINARY")
    if not binary:
        harness = env.get("HERALD_HARNESS", "opencode").strip().lower()
        binary = {"opencode": "opencode", "claude": "claude", "codex": "codex"}.get(harness)
    if not binary:
        return None
    resolved = shutil.which(binary)
    return str(Path(resolved).resolve().parent) if resolved else None


__all__ = [
    "BubblewrapSandbox",
    "NoSandbox",
    "Sandbox",
    "SandboxError",
    "SandboxPolicy",
    "sandbox_policy_from_env",
]

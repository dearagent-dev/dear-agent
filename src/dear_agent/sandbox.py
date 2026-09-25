from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


class SandboxError(RuntimeError):
    """A sandbox could not wrap a command."""


@dataclass(slots=True, frozen=True)
class Mount:
    """One host path exposed inside a container.

    ``host`` may start with ``~`` (the host home). ``container`` may start with ``~`` to mean
    the image's home (``ContainerSandbox.container_home``), so the same metadata works for an
    image whose user is ``root`` or ``node``. ``relabel`` requests an SELinux ``Z`` relabel of
    this mount even under the ``auto`` policy; use it for paths Dear Agent owns (the harness
    bundle), never for the operator's credential files.
    """

    host: str
    container: str
    readonly: bool = True
    relabel: bool = False


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
    """Wraps a command so it runs under an OS sandbox.

    ``wrap`` maps an argv to the argv that runs it inside the jail. For a container jail the
    first ``wrap`` starts a long-lived environment **session** and later calls ``exec`` into it,
    so a harness and the ``verify`` gate that follows share one environment (ADR 0008).
    ``close`` ends the session; it is a no-op for jails without one.
    """

    def wrap(self, argv: list[str], *, worktree: Path) -> list[str]: ...

    @property
    def available(self) -> bool: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class NoSandbox:
    """Runs the command as-is. Used when no OS sandbox is available."""

    @property
    def available(self) -> bool:
        return False

    def wrap(self, argv: list[str], *, worktree: Path) -> list[str]:
        return list(argv)

    def close(self) -> None:
        return None


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

    def close(self) -> None:
        # bubblewrap is per-command; there is no session to end.
        return None


DEFAULT_CONTAINER_BINARY = "podman"


@dataclass(slots=True)
class ContainerSandbox:
    """Wraps a command in an OCI container (``podman`` by default).

    Unlike ``bwrap``, the image brings the whole runtime, so nothing has to be installed on the
    host. The worktree is bind-mounted read-write at ``workdir`` — the container writes there,
    and only there, so no source code has to leave the host (golden rule 1). Credentials are
    exposed through ``mounts``; each is read-only unless the mount says otherwise, so a harness
    cannot rewrite the operator's config.

    The sandbox is a **session** (ADR 0008): the first ``wrap`` starts a long-lived container
    and later calls ``exec`` into it, so the harness and the ``verify`` gate share one
    environment. The image may be the harness's own, or a repository-declared environment into
    which the harness is injected (``environment/bundle.py``).

    ``container_home`` is the image's home: a mount whose container path starts with ``~`` is
    resolved against it, so the same metadata works for an image whose user is ``root`` or
    ``node``.
    """

    image: str
    mounts: tuple[Mount, ...] = ()
    env_allowlist: tuple[str, ...] = ()
    network: str = "host"
    container_home: str = "/root"
    workdir: str = "/work"
    binary: str = DEFAULT_CONTAINER_BINARY
    userns_keep_id: bool = False
    extra_args: tuple[str, ...] = ()
    cap_drop: bool = True
    pids_limit: int | None = 512
    read_only: bool = False
    # argv lists run in the session, in order, right after it starts and before the harness
    # (ADR 0008 bootstrap): how a harness is installed into an environment that lacks it.
    setup: tuple[tuple[str, ...], ...] = ()
    # SELinux handling for the bind mounts (Fedora/RHEL/OpenShift run enforcing):
    #   auto    - relabel the disposable worktree with :Z when SELinux is enabled; leave the
    #             credential mounts alone (relabel them explicitly with ``Z``/``z`` if needed)
    #   Z / z   - relabel every mount (private ``Z`` / shared ``z``); mutates the host label
    #   disable - pass ``--security-opt label=disable`` and relabel nothing
    #   none    - never touch SELinux labels
    selinux: str = "auto"
    # The live session's container name, or ``None`` before the first ``wrap`` (ADR 0008).
    # Not part of equality: two sandboxes with the same configuration are the same sandbox.
    _container: str | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.selinux not in _SELINUX_MODES:
            raise SandboxError(
                f"invalid selinux mode {self.selinux!r}; expected one of "
                f"{', '.join(sorted(_SELINUX_MODES))}"
            )
        if self.pids_limit is not None and self.pids_limit <= 0:
            raise SandboxError(f"pids_limit must be positive, got {self.pids_limit}")

    @property
    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def wrap(self, argv: list[str], *, worktree: Path) -> list[str]:
        """Run ``argv`` inside the task's environment session.

        The first call starts a long-lived container; every later call (the ``verify`` gate that
        follows the harness) executes in that same container, so anything the harness installed
        is still there (ADR 0008). The caller ends the session with :meth:`close`.
        """
        if not self.available:
            raise SandboxError(f"{self.binary!r} is not available")
        self._ensure_started(worktree)
        return self.build_exec_argv(argv)

    def build_wrapped_argv(self, argv: list[str], *, worktree: Path) -> list[str]:
        """Build a one-shot ``podman run --rm`` invocation (the per-command contract)."""
        return self._run_argv(argv, worktree=worktree, remove=True, name=None)

    def build_exec_argv(self, argv: list[str]) -> list[str]:
        """Build the ``podman exec`` that runs ``argv`` in the live session."""
        if self._container is None:
            raise SandboxError("session has not been started")
        command: list[str] = [self.binary, "exec", "--workdir", self.workdir]
        for env_name in self.env_allowlist:
            if env_name in os.environ:
                command += ["--env", f"{env_name}={os.environ[env_name]}"]
        # Tools (OpenCode) resolve the project from PWD; set it to the worktree inside.
        command += ["--env", f"PWD={self.workdir}"]
        command += [self._container]
        command += argv
        return command

    def _ensure_started(self, worktree: Path) -> None:
        if self._container is not None:
            return
        name = f"dear-agent-{uuid.uuid4().hex}"
        argv = self._run_argv(
            ["infinity"],
            worktree=worktree,
            remove=False,
            name=name,
            detach=True,
            entrypoint="sleep",
        )
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise SandboxError(result.stderr.strip() or f"cannot start the {self.image!r} session")
        self._container = name
        for command in self.setup:
            self._run_setup(command)

    def _run_setup(self, command: tuple[str, ...]) -> None:
        """Run one bootstrap command in the live session; fail loudly if it does not work."""
        result = subprocess.run(
            self.build_exec_argv(list(command)), capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise SandboxError(f"session setup {command!r} failed: {detail[:300]}")

    def close(self) -> None:
        """End the session and remove the container. Safe to call more than once."""
        if self._container is None:
            return
        name, self._container = self._container, None
        subprocess.run([self.binary, "rm", "-f", name], capture_output=True, text=True, check=False)

    def _run_argv(
        self,
        argv: list[str],
        *,
        worktree: Path,
        remove: bool,
        name: str | None,
        detach: bool = False,
        entrypoint: str | None = None,
    ) -> list[str]:
        """Build the container invocation without checking availability."""
        command: list[str] = [self.binary, "run"]
        if detach:
            command += ["-d", "--name", name]
        if remove:
            command.append("--rm")
        command += ["--security-opt", "no-new-privileges"]
        if self.cap_drop:
            command += ["--cap-drop", "ALL"]
        if self.pids_limit is not None:
            command += ["--pids-limit", str(self.pids_limit)]
        if self.read_only:
            command += ["--read-only", "--tmpfs", "/tmp"]
        command += [
            "--volume",
            f"{worktree}:{self.workdir}:rw{self._worktree_relabel()}",
            "--workdir",
            self.workdir,
            "--network",
            self.network,
        ]
        if self.selinux == "disable":
            command += ["--security-opt", "label=disable"]
        if self.userns_keep_id:
            command.append("--userns=keep-id")
        for mount in self.mounts:
            host = str(Path(mount.host).expanduser())
            container = _container_path(mount.container, self.container_home)
            mode = "ro" if mount.readonly else "rw"
            command += ["--volume", f"{host}:{container}:{mode}{self._mount_relabel(mount)}"]
        for env_name in self.env_allowlist:
            if env_name in os.environ:
                command += ["--env", f"{env_name}={os.environ[env_name]}"]
        command += list(self.extra_args)
        if entrypoint:
            # Override the image entrypoint so the session stays alive (e.g. `sleep infinity`).
            command += ["--entrypoint", entrypoint]
        command += [self.image]
        command += argv
        return command

    def _worktree_relabel(self) -> str:
        if self.selinux in ("z", "Z"):
            return f",{self.selinux}"
        if self.selinux == "auto" and _selinux_enabled():
            return ",Z"
        return ""

    def _mount_relabel(self, mount: Mount) -> str:
        if self.selinux in ("z", "Z"):
            return f",{self.selinux}"
        if mount.relabel and self.selinux == "auto" and _selinux_enabled():
            return ",Z"
        return ""


_SELINUX_MODES = frozenset({"auto", "none", "z", "Z", "disable"})


def _selinux_enabled() -> bool:
    return Path("/sys/fs/selinux").exists()


def _container_path(path: str, container_home: str) -> str:
    if path == "~":
        return container_home
    if path.startswith("~/"):
        return f"{container_home}/{path[2:]}"
    return path


def parse_mounts(value: str | None) -> list[Mount]:
    """Parse ``host:container[:ro|rw]`` entries (comma- or semicolon-separated).

    Both sides may start with ``~`` (host home, or the image home on the container side).
    """
    mounts: list[Mount] = []
    for entry in _split_paths(value):
        host, sep, rest = entry.partition(":")
        if not sep or not host.strip() or not rest.strip():
            raise SandboxError(f"invalid mount {entry!r}; expected host:container[:ro|rw]")
        container, _, mode = rest.partition(":")
        mode = mode.strip().lower()
        if mode not in ("", "ro", "rw"):
            raise SandboxError(f"invalid mount mode {mode!r} in {entry!r}")
        mounts.append(Mount(host=host.strip(), container=container.strip(), readonly=mode != "rw"))
    return mounts


def sandbox_policy_from_env(env: Mapping[str, str] | None = None) -> SandboxPolicy:
    """Build the sandbox policy from ``DEAR_AGENT_SANDBOX_*``.

    - ``DEAR_AGENT_SANDBOX_NETWORK=true`` lets a run reach the model; otherwise egress is denied
      (safest, but a hosted model will not answer).
    - ``DEAR_AGENT_SANDBOX_READABLE`` adds read-only paths (e.g. a harness installed outside
      ``/usr``); the resolved harness binary's directory is added automatically.
    - ``DEAR_AGENT_SANDBOX_WRITABLE`` adds writable paths beyond the worktree (avoid).
    - ``DEAR_AGENT_SANDBOX_ENV`` adds environment variables to pass through — a secret listed
      here becomes visible to the run, so keep it to non-secrets.

    ``$HOME`` is never mounted; credentials in ``~/.ssh`` or ``~/.config`` stay invisible.
    """
    source = env if env is not None else os.environ
    defaults = SandboxPolicy()
    readable = list(defaults.readable_paths) + _split_paths(
        source.get("DEAR_AGENT_SANDBOX_READABLE")
    )
    binary_dir = _harness_dir(source)
    if binary_dir:
        readable.append(binary_dir)
    env_names = list(defaults.env_allowlist) + _split_paths(source.get("DEAR_AGENT_SANDBOX_ENV"))
    return SandboxPolicy(
        allow_network=source.get("DEAR_AGENT_SANDBOX_NETWORK", "false").lower() == "true",
        readable_paths=tuple(dict.fromkeys(readable)),
        writable_paths=tuple(_split_paths(source.get("DEAR_AGENT_SANDBOX_WRITABLE"))),
        env_allowlist=tuple(dict.fromkeys(env_names)),
    )


def _split_paths(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").replace(";", ",").split(",") if part.strip()]


def _harness_dir(env: Mapping[str, str]) -> str | None:
    binary = env.get("DEAR_AGENT_HARNESS_BINARY")
    if not binary:
        harness = env.get("DEAR_AGENT_HARNESS", "opencode").strip().lower()
        binary = {"opencode": "opencode", "claude": "claude", "codex": "codex"}.get(harness)
    if not binary:
        return None
    resolved = shutil.which(binary)
    return str(Path(resolved).resolve().parent) if resolved else None


__all__ = [
    "BubblewrapSandbox",
    "ContainerSandbox",
    "Mount",
    "NoSandbox",
    "Sandbox",
    "SandboxError",
    "SandboxPolicy",
    "parse_mounts",
    "sandbox_policy_from_env",
]

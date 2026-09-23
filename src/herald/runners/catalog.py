from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from herald.runners.port import Runner
from herald.sandbox import ContainerSandbox, Mount, Sandbox, parse_mounts


@dataclass(frozen=True, slots=True)
class HarnessInfo:
    """Metadata for one harness Herald can wrap.

    ``accepts_model`` is the important bit: a harness that takes a model flag (OpenCode via
    ``--model``) can be pointed at a provider/model; one that authenticates by subscription
    (Claude Code, Codex) must not be forced a model, or the run fails. Herald adds a model
    only when the harness accepts one.

    ``image``/``mounts``/``container_env`` describe how to run the harness in a container
    (``HERALD_ISOLATION=podman``). The image is a configuration detail (golden rule 5): a
    harness with an official image ships one as a default, and any harness can be pointed at
    an operator-supplied image with ``HERALD_HARNESS_IMAGE``. A harness with no default image
    (a subscription harness with no official image) simply cannot run containerised until one
    is configured.
    """

    id: str
    binary: str
    accepts_model: bool = False
    local: bool = True
    free: bool = False
    image: str | None = None
    mounts: tuple[Mount, ...] = ()
    container_env: tuple[str, ...] = ()


@runtime_checkable
class HarnessCatalog(Protocol):
    """Lists the harnesses available to this deployment, with the metadata to choose among them."""

    def list_harnesses(self) -> list[HarnessInfo]: ...


@dataclass(slots=True)
class EnvHarnessCatalog:
    """The in-tree harnesses, read from the environment.

    The three first-class adapters are always listed; a ``command`` entry appears only when
    ``HERALD_HARNESS_COMMAND`` is set, because an operator must explicitly choose an arbitrary
    harness argv (it is never derived from an inbound message).
    """

    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    def list_harnesses(self) -> list[HarnessInfo]:
        infos = [
            HarnessInfo(
                id="opencode",
                binary="opencode",
                accepts_model=True,
                image="ghcr.io/anomalyco/opencode:latest",
                mounts=(Mount("~/.local/share/opencode", "~/.local/share/opencode"),),
                container_env=("OPENCODE_CONFIG_CONTENT",),
            ),
            HarnessInfo(
                id="claude",
                binary="claude",
                accepts_model=False,
                mounts=(
                    Mount("~/.claude", "~/.claude", False),
                    Mount("~/.claude.json", "~/.claude.json", False),
                ),
            ),
            HarnessInfo(id="codex", binary="codex", accepts_model=False),
        ]
        command = self.env.get("HERALD_HARNESS_COMMAND")
        if command:
            infos.append(HarnessInfo(id="command", binary=command, accepts_model=False))
        return infos


DEFAULT_HARNESS = "opencode"
_AUTO = {"auto", "local-agent"}


def select_harness(
    catalog: HarnessCatalog,
    preferred: str | None = None,
    *,
    which: Callable[[str], str | None] | None = None,
) -> HarnessInfo:
    """Pick a harness by id; ``auto``/``local-agent`` picks the first one on ``PATH``."""
    which = which or shutil.which  # resolved per call, so a test can patch shutil.which
    infos = {info.id: info for info in catalog.list_harnesses()}
    name = (preferred or DEFAULT_HARNESS).strip().lower()

    if name in _AUTO:
        for info in infos.values():
            if info.id != "command" and which(info.binary):
                return info
        raise RuntimeError(
            "no local agent found on PATH (looked for opencode, claude, codex); "
            "set HERALD_HARNESS and HERALD_HARNESS_BINARY"
        )

    if name == "command" and "command" not in infos:
        raise RuntimeError("HERALD_HARNESS_COMMAND is required for HERALD_HARNESS=command")
    info = infos.get(name)
    if info is None:
        raise RuntimeError(f"unknown harness {name!r}")
    return info


def container_sandbox(info: HarnessInfo, env: Mapping[str, str]) -> ContainerSandbox:
    """Build the container sandbox for ``info`` from its metadata and ``HERALD_HARNESS_*``.

    The image, mounts and container env default to the harness metadata; each is overridden
    wholesale by ``HERALD_HARNESS_IMAGE``, ``HERALD_HARNESS_MOUNTS`` and
    ``HERALD_HARNESS_CONTAINER_ENV``. The remaining knobs (network, container home, workdir,
    container binary, userns, extra args) come from ``HERALD_HARNESS_CONTAINER_*`` /
    ``HERALD_CONTAINER_BINARY``. A harness with no default image needs
    ``HERALD_HARNESS_IMAGE`` set, or the run fails loudly rather than silently unsandboxed.
    """
    image = env.get("HERALD_HARNESS_IMAGE") or info.image
    if not image:
        raise RuntimeError(f"harness {info.id!r} has no container image; set HERALD_HARNESS_IMAGE")
    mounts = parse_mounts(env.get("HERALD_HARNESS_MOUNTS")) or list(info.mounts)
    container_env = _split_list(env.get("HERALD_HARNESS_CONTAINER_ENV")) or list(info.container_env)
    return ContainerSandbox(
        image=image,
        mounts=tuple(mounts),
        env_allowlist=tuple(container_env),
        network=env.get("HERALD_HARNESS_CONTAINER_NETWORK", "host"),
        container_home=env.get("HERALD_HARNESS_CONTAINER_HOME", "/root"),
        workdir=env.get("HERALD_HARNESS_CONTAINER_WORKDIR", "/work"),
        binary=env.get("HERALD_CONTAINER_BINARY", "podman"),
        userns_keep_id=env.get("HERALD_HARNESS_CONTAINER_USERNS", "").lower() == "keep-id",
        extra_args=tuple(_split_list(env.get("HERALD_HARNESS_CONTAINER_ARGS"))),
    )


def _split_list(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def build_runner_for(info: HarnessInfo, model: str | None, sandbox: Sandbox) -> Runner:
    """Build the runner for ``info``, adding the model only when the harness accepts one."""
    harness_model = model if info.accepts_model else None

    if info.id == "command":
        from herald.runners.command import CommandRunner

        return CommandRunner(command=info.binary.split())
    if info.id == "claude":
        from herald.runners.claude import ClaudeCodeRunner

        return ClaudeCodeRunner(model=harness_model, binary=info.binary, sandbox=sandbox)
    if info.id == "codex":
        from herald.runners.codex import CodexRunner

        return CodexRunner(model=harness_model, binary=info.binary, sandbox=sandbox)
    if info.id == "opencode":
        from herald.runners.opencode import OpenCodeRunner

        return OpenCodeRunner(model=harness_model, binary=info.binary, sandbox=sandbox)
    raise RuntimeError(f"unknown harness {info.id!r}")


__all__ = [
    "DEFAULT_HARNESS",
    "EnvHarnessCatalog",
    "HarnessCatalog",
    "HarnessInfo",
    "build_runner_for",
    "container_sandbox",
    "select_harness",
]

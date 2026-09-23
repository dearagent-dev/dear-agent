from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from herald.runners.port import Runner
from herald.sandbox import Sandbox


@dataclass(frozen=True, slots=True)
class HarnessInfo:
    """Metadata for one harness Herald can wrap.

    ``accepts_model`` is the important bit: a harness that takes a model flag (OpenCode via
    ``--model``) can be pointed at a provider/model; one that authenticates by subscription
    (Claude Code, Codex) must not be forced a model, or the run fails. Herald adds a model
    only when the harness accepts one.
    """

    id: str
    binary: str
    accepts_model: bool = False
    local: bool = True
    free: bool = False


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
            HarnessInfo(id="opencode", binary="opencode", accepts_model=True),
            HarnessInfo(id="claude", binary="claude", accepts_model=False),
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


def build_runner_for(info: HarnessInfo, model: str | None, sandbox: Sandbox) -> Runner:
    """Build the runner for ``info``, adding the model only when the harness accepts one."""
    harness_model = model if info.accepts_model else None

    if info.id == "command":
        from herald.runners.command import CommandRunner

        return CommandRunner(command=info.binary.split())
    if info.id == "claude":
        from herald.runners.claude import ClaudeCodeRunner

        return ClaudeCodeRunner(model=harness_model, binary=info.binary)
    if info.id == "codex":
        from herald.runners.codex import CodexRunner

        return CodexRunner(model=harness_model, binary=info.binary)
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
    "select_harness",
]

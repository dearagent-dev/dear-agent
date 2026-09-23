from __future__ import annotations

from dataclasses import dataclass

from dear_agent.queue.models import TaskSpec
from dear_agent.runners.harness import HarnessRunner

DEFAULT_BINARY = "opencode"


@dataclass(slots=True)
class OpenCodeRunner(HarnessRunner):
    """Runner adapter for the OpenCode harness (``opencode run``)."""

    binary: str = DEFAULT_BINARY

    def build_argv(self, spec: TaskSpec) -> list[str]:
        argv = [self.binary, "run"]
        if self.model:
            argv += ["--model", self.model]
        argv += [spec.instructions]
        return argv

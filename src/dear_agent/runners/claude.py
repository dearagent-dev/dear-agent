from __future__ import annotations

from dataclasses import dataclass

from dear_agent.queue.models import TaskSpec
from dear_agent.runners.harness import HarnessRunner

DEFAULT_BINARY = "claude"


@dataclass(slots=True)
class ClaudeCodeRunner(HarnessRunner):
    """Runner adapter for Claude Code (``claude -p <prompt>``).

    The print/non-interactive mode is used so a run never waits on a TTY.
    """

    binary: str = DEFAULT_BINARY

    def build_argv(self, spec: TaskSpec) -> list[str]:
        argv = [self.binary, "-p"]
        if self.model:
            argv += ["--model", self.model]
        argv += [spec.instructions]
        return argv

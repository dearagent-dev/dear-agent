from __future__ import annotations

from dataclasses import dataclass

from herald.queue.models import TaskSpec
from herald.runners.harness import HarnessRunner

DEFAULT_BINARY = "codex"


@dataclass(slots=True)
class CodexRunner(HarnessRunner):
    """Runner adapter for Codex (``codex exec <prompt>``)."""

    binary: str = DEFAULT_BINARY

    def build_argv(self, spec: TaskSpec) -> list[str]:
        argv = [self.binary, "exec"]
        if self.model:
            argv += ["--model", self.model]
        argv += [spec.instructions]
        return argv

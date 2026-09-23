from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TextIO

from herald.queue.factory import build_queue
from herald.queue.port import Queue


@dataclass(slots=True)
class CliContext:
    queue: Queue | None
    as_json: bool
    out: TextIO

    def require_queue(self) -> Queue:
        if self.queue is None:
            raise RuntimeError("this command requires a queue backend")
        return self.queue

    def emit(self, message: str) -> None:
        print(message, file=self.out)

    def emit_json(self, payload: Any) -> None:
        import json

        print(json.dumps(payload, indent=2, default=str), file=self.out)


__all__ = ["CliContext", "build_queue"]

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from dear_agent.queue.models import Task, TaskSpec
from dear_agent.runners.port import Runner
from dear_agent.runners.worktree import RunResult, Worktree

REQUEST_FILE = "harness-request.json"
RESULT_FILE = "harness-result.json"
DEFAULT_POLL_SECONDS = 0.2


@dataclass(slots=True)
class HarnessRequest:
    """What the control container asks the harness container to run.

    Deliberately carries no credential: only the task's instructions, the shared worktree path
    and the branch. The harness container builds its own runner from its environment (which
    holds no Dear Agent secret), so nothing sensitive crosses the shared volume.
    """

    task_id: str
    instructions: str
    model_request: str | None
    workdir: str
    branch: str

    def dumps(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def loads(cls, text: str) -> HarnessRequest:
        data = json.loads(text)
        return cls(
            task_id=data["task_id"],
            instructions=data.get("instructions", ""),
            model_request=data.get("model_request"),
            workdir=data["workdir"],
            branch=data.get("branch", "dear-agent"),
        )


class DelegatingRunner:
    """Control-side :class:`Runner` that hands the harness to a sibling container.

    The two containers share a directory (an ``emptyDir``); this writes a request and waits
    for the harness container's result. The control container keeps the credentials and the
    worktree; the harness container only sees the shared worktree.
    """

    def __init__(
        self,
        shared_dir: str | Path,
        *,
        timeout: float = 3600.0,
        poll: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._dir = Path(shared_dir)
        self._timeout = timeout
        self._poll = poll

    def run(self, task: Task, spec: TaskSpec, worktree: Worktree) -> RunResult:
        self._dir.mkdir(parents=True, exist_ok=True)
        request_path = self._dir / REQUEST_FILE
        result_path = self._dir / RESULT_FILE
        result_path.unlink(missing_ok=True)
        request_path.write_text(
            HarnessRequest(
                task_id=task.id,
                instructions=spec.instructions,
                model_request=spec.model_request,
                workdir=str(worktree.path),
                branch=worktree.branch,
            ).dumps()
        )

        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if result_path.exists():
                result = RunResult(**json.loads(result_path.read_text()))
                result_path.unlink(missing_ok=True)
                return result
            time.sleep(self._poll)
        return RunResult(
            exit_code=124,
            stderr="the harness container did not answer in time",
            branch=worktree.branch,
        )


def serve(
    shared_dir: str | Path,
    *,
    runner: Runner,
    poll: float = DEFAULT_POLL_SECONDS,
    once: bool = False,
) -> None:
    """Harness-side loop: run each request that appears in the shared directory.

    Runs without a sandbox (the container itself is the jail) and holds no credential. Blocks
    for the pod's lifetime, or until one request when ``once`` (tests).
    """
    directory = Path(shared_dir)
    directory.mkdir(parents=True, exist_ok=True)
    request_path = directory / REQUEST_FILE
    result_path = directory / RESULT_FILE

    while True:
        if not request_path.exists():
            time.sleep(poll)
            continue

        request = HarnessRequest.loads(request_path.read_text())
        worktree = Worktree(
            repo_path=Path(request.workdir), path=Path(request.workdir), branch=request.branch
        )
        task = Task(id=request.task_id, transport_id=request.task_id)
        spec = TaskSpec(
            repo_url="", instructions=request.instructions, model_request=request.model_request
        )
        try:
            result = runner.run(task, spec, worktree)
        except Exception as exc:  # noqa: BLE001 - report a broken harness; keep serving
            result = RunResult(exit_code=1, stderr=f"harness raised {type(exc).__name__}")
        result_path.write_text(json.dumps(asdict(result)))
        request_path.unlink(missing_ok=True)
        if once:
            return


__all__ = ["DelegatingRunner", "HarnessRequest", "serve"]

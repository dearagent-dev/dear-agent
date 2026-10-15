"""The two-container runner protocol (ADR 0007).

The **control** container holds the credentials (database DSN, Git keys, forge token) and the
worktree; the **harness** container holds only the model credential. They share one directory
and talk over it with small JSON files.

Two rules keep that boundary honest:

* **Everything that executes repository-controlled code runs on the harness side.** That is the
  harness itself *and* the task's ``verify`` command (which the harness can influence: it edits
  the worktree the command runs in). The control container only writes the request and reads the
  result, so a malicious ``Makefile`` cannot reach the control container's credentials.
* **The handshake is atomic.** Requests and results are written to a temporary file and renamed
  into place, and every message carries a monotonic sequence number, so a half-written file is
  never read and a stale result is never mistaken for the current one.

The harness container serves until the control container writes the shutdown marker (which it
does in a ``finally``), so a run can make *several* requests: the harness, a revision after an
adversarial review, and one or more ``verify`` commands.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from dear_agent.queue.models import Task, TaskSpec
from dear_agent.runners.harness import harness_env
from dear_agent.runners.port import Runner
from dear_agent.runners.worktree import RunResult, Worktree

REQUEST_FILE = "harness-request.json"
RESULT_FILE = "harness-result.json"
COMMAND_REQUEST_FILE = "command-request.json"
COMMAND_RESULT_FILE = "command-result.json"
SHUTDOWN_FILE = "harness-shutdown"

DEFAULT_POLL_SECONDS = 0.2
DEFAULT_IDLE_TIMEOUT_SECONDS = 900.0


class SidecarError(RuntimeError):
    """The two-container handshake failed."""


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so a reader never sees a partial file."""
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temp.write_text(text)
    os.replace(temp, path)


def _read_json(path: Path) -> dict:
    text = path.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SidecarError(f"{path.name} is not valid JSON: {exc}") from exc


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
    seq: int = 0

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
            seq=int(data.get("seq", 0)),
        )


@dataclass(slots=True)
class CommandRequest:
    """A command for the harness container to execute on the control container's behalf.

    This is how the ``verify`` gate stays out of the credential holder: the control container
    decides *which* command may run (its allowlist is unchanged), and the harness container —
    which has no database DSN, Git key or forge token — runs it on files it wrote itself.
    """

    argv: list[str]
    cwd: str
    timeout: float
    seq: int = 0

    def dumps(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def loads(cls, text: str) -> CommandRequest:
        data = json.loads(text)
        return cls(
            argv=[str(item) for item in data["argv"]],
            cwd=data["cwd"],
            timeout=float(data.get("timeout", 0)),
            seq=int(data.get("seq", 0)),
        )


@dataclass(slots=True)
class CommandResult:
    """The outcome of a :class:`CommandRequest`."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""

    def dumps(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def loads(cls, text: str) -> CommandResult:
        data = json.loads(text)
        return cls(
            exit_code=int(data.get("exit_code", 1)),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
        )


class SidecarSession:
    """Control-side client of the harness container.

    Owns the shared directory and the sequence counter. :meth:`run_harness` and
    :meth:`run_command` block until the harness container answers; :meth:`close` tells it to
    stop. The executor owns the session's lifetime (it closes it in a ``finally``), so a failed
    run never leaves the harness container waiting.
    """

    def __init__(
        self,
        shared_dir: str | Path,
        *,
        timeout: float = 3600.0,
        poll: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.dir = Path(shared_dir)
        self._timeout = timeout
        self._poll = poll
        self._seq = 0

    # -- requests ---------------------------------------------------------------------------
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _request(self, request_file: str, result_file: str, payload: str, seq: int) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        request_path = self.dir / request_file
        result_path = self.dir / result_file
        result_path.unlink(missing_ok=True)
        _write_atomic(request_path, payload)

        deadline = time.monotonic() + self._timeout
        while True:
            if result_path.exists():
                result = _read_json(result_path)
                if int(result.get("seq", -1)) != seq:
                    # A stale answer from a previous request: drop it and keep waiting.
                    result_path.unlink(missing_ok=True)
                else:
                    result_path.unlink(missing_ok=True)
                    return result
            if time.monotonic() >= deadline:
                raise SidecarError(f"the harness container did not answer {request_file} in time")
            time.sleep(self._poll)

    def run_harness(self, task: Task, spec: TaskSpec, worktree: Worktree) -> RunResult:
        seq = self._next_seq()
        request = HarnessRequest(
            task_id=task.id,
            instructions=spec.instructions,
            model_request=spec.model_request,
            workdir=str(worktree.path),
            branch=worktree.branch,
            seq=seq,
        )
        try:
            result = self._request(REQUEST_FILE, RESULT_FILE, request.dumps(), seq)
        except SidecarError as exc:
            return RunResult(exit_code=124, stderr=str(exc), branch=worktree.branch)
        result.pop("seq", None)
        return RunResult(**result)

    def run_command(
        self, argv: list[str], cwd: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Run ``argv`` in the harness container. Matches ``verify.Executor``'s signature."""
        seq = self._next_seq()
        request = CommandRequest(argv=list(argv), cwd=cwd, timeout=float(timeout), seq=seq)
        try:
            result = self._request(COMMAND_REQUEST_FILE, COMMAND_RESULT_FILE, request.dumps(), seq)
        except SidecarError as exc:
            # A dead harness container must read as a failed command, not as a crash inside the
            # verifier: the executor turns this into VERIFY_FAILED.
            return subprocess.CompletedProcess(
                args=list(argv), returncode=124, stdout="", stderr=str(exc)
            )
        return subprocess.CompletedProcess(
            args=list(argv),
            returncode=int(result.get("exit_code", 1)),
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
        )

    def close(self) -> None:
        """Ask the harness container to stop serving. Idempotent."""
        if not self.dir.exists():
            return
        try:
            _write_atomic(self.dir / SHUTDOWN_FILE, "shutdown\n")
        except OSError:
            return


@dataclass(slots=True)
class DelegatingRunner:
    """Control-side :class:`Runner` that hands the harness to the sibling container."""

    session: SidecarSession

    def run(self, task: Task, spec: TaskSpec, worktree: Worktree) -> RunResult:
        return self.session.run_harness(task, spec, worktree)


def serve_command(request: CommandRequest) -> CommandResult:
    """Run a verify command on the harness side (no credential lives here)."""
    try:
        completed = subprocess.run(
            list(request.argv),
            cwd=request.cwd,
            env=harness_env(request.cwd),
            capture_output=True,
            text=True,
            timeout=request.timeout or None,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(exit_code=127, stderr=f"{request.argv[0]!r} not found")
    except subprocess.TimeoutExpired:
        return CommandResult(exit_code=124, stderr="the verify command timed out")
    except OSError as exc:
        return CommandResult(exit_code=126, stderr=str(exc))
    return CommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def serve(
    shared_dir: str | Path,
    *,
    runner: Runner,
    poll: float = DEFAULT_POLL_SECONDS,
    once: bool = False,
    idle_timeout: float | None = None,
) -> None:
    """Harness-side loop: answer requests until the control container shuts it down.

    Runs without a sandbox (the container itself is the jail) and holds no credential. It
    answers *both* harness requests and ``verify`` commands, so a run can be revised and
    verified before the control container publishes. ``once`` exits after one harness request
    (tests); otherwise the loop ends on the shutdown marker or after ``idle_timeout`` seconds
    without a request, so a killed control container cannot leave it waiting forever.
    """
    directory = Path(shared_dir)
    directory.mkdir(parents=True, exist_ok=True)
    shutdown_path = directory / SHUTDOWN_FILE
    request_path = directory / REQUEST_FILE
    result_path = directory / RESULT_FILE
    command_path = directory / COMMAND_REQUEST_FILE
    command_result_path = directory / COMMAND_RESULT_FILE
    idle_limit = idle_timeout if idle_timeout is not None else DEFAULT_IDLE_TIMEOUT_SECONDS
    last_request_at = time.monotonic()

    while True:
        if shutdown_path.exists():
            return
        if request_path.exists():
            last_request_at = time.monotonic()
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
            # Print what the harness did: this container's log is the operator's (and the
            # demo console's) window onto the agent, and on a failure it is the only place the
            # reason is visible.
            print(f"[dear-agent] harness exit={result.exit_code}", flush=True)
            for line in (result.stdout or "").strip().splitlines()[-40:]:
                print(f"  {line}", flush=True)
            for line in (result.stderr or "").strip().splitlines()[-40:]:
                print(f"  {line}", flush=True)
            payload = asdict(result)
            payload["seq"] = request.seq
            # Write the result before removing the request: a crash in between is retried,
            # never lost.
            _write_atomic(result_path, json.dumps(payload))
            request_path.unlink(missing_ok=True)
            if once:
                return
            continue
        if command_path.exists():
            last_request_at = time.monotonic()
            request = CommandRequest.loads(command_path.read_text())
            result = serve_command(request)
            payload = asdict(result)
            payload["seq"] = request.seq
            _write_atomic(command_result_path, json.dumps(payload))
            command_path.unlink(missing_ok=True)
            continue
        if idle_limit > 0 and time.monotonic() - last_request_at > idle_limit:
            return
        time.sleep(poll)


__all__ = [
    "COMMAND_REQUEST_FILE",
    "COMMAND_RESULT_FILE",
    "DEFAULT_IDLE_TIMEOUT_SECONDS",
    "DEFAULT_POLL_SECONDS",
    "REQUEST_FILE",
    "RESULT_FILE",
    "SHUTDOWN_FILE",
    "CommandRequest",
    "CommandResult",
    "DelegatingRunner",
    "HarnessRequest",
    "SidecarError",
    "SidecarSession",
    "serve",
    "serve_command",
]

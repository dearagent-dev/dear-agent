from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from dear_agent.queue.models import Task, TaskSpec
from dear_agent.runners.command import CommandRunner
from dear_agent.runners.shared import (
    COMMAND_REQUEST_FILE,
    REQUEST_FILE,
    RESULT_FILE,
    SHUTDOWN_FILE,
    DelegatingRunner,
    SidecarSession,
    serve,
)
from dear_agent.runners.worktree import Worktree
from dear_agent.verify import CommandVerifier


def _worktree(tmp_path: Path) -> Worktree:
    workdir = tmp_path / "wt"
    workdir.mkdir()
    return Worktree(repo_path=workdir, path=workdir, branch="dear-agent/x")


def _serve_in_background(shared: Path, runner, **kwargs) -> threading.Thread:
    thread = threading.Thread(target=serve, args=(shared,), kwargs={"runner": runner, **kwargs})
    thread.daemon = True
    thread.start()
    return thread


def test_delegating_runner_round_trips(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    harness = CommandRunner(command=["python", "-c", "print('harness ran')"])
    thread = _serve_in_background(shared, harness, once=True)

    session = SidecarSession(shared, timeout=10)
    result = DelegatingRunner(session).run(
        Task(id="e1", transport_id="<m1@x>"),
        TaskSpec(repo_url="", instructions="do it"),
        _worktree(tmp_path),
    )
    thread.join(timeout=10)

    assert result.exit_code == 0
    assert "harness ran" in result.stdout


def test_delegating_runner_times_out_when_nothing_answers(tmp_path: Path) -> None:
    session = SidecarSession(tmp_path / "shared", timeout=0.4, poll=0.05)

    result = DelegatingRunner(session).run(
        Task(id="e1", transport_id="<m1@x>"),
        TaskSpec(repo_url="", instructions="x"),
        _worktree(tmp_path),
    )

    assert result.exit_code == 124


def test_the_request_carries_no_credentials(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    # No server: the run times out and leaves the request file for inspection.
    session = SidecarSession(shared, timeout=0.2, poll=0.05)
    DelegatingRunner(session).run(
        Task(id="e1", transport_id="<m1@x>"),
        TaskSpec(repo_url="", instructions="do it", model_request="local:x"),
        _worktree(tmp_path),
    )

    text = (shared / REQUEST_FILE).read_text()
    for forbidden in ("DATABASE_URL", "SSH", "TOKEN", "PASSWORD", "SECRET"):
        assert forbidden not in text
    assert set(json.loads(text)) == {
        "task_id",
        "instructions",
        "model_request",
        "workdir",
        "branch",
        "seq",
    }


def test_the_harness_serves_several_requests_until_shutdown(tmp_path: Path) -> None:
    """A revision after a review is a second harness request in the same container."""
    shared = tmp_path / "shared"
    harness = CommandRunner(command=["python", "-c", "print('ran')"])
    thread = _serve_in_background(shared, harness, once=False, poll=0.02, idle_timeout=30)
    session = SidecarSession(shared, timeout=10, poll=0.02)
    worktree = _worktree(tmp_path)
    task = Task(id="e1", transport_id="<m1@x>")

    first = DelegatingRunner(session).run(task, TaskSpec(repo_url="", instructions="one"), worktree)
    second = DelegatingRunner(session).run(
        task, TaskSpec(repo_url="", instructions="two"), worktree
    )
    session.close()
    thread.join(timeout=10)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert not thread.is_alive()


def test_the_verify_command_runs_on_the_harness_side(tmp_path: Path) -> None:
    """The gate must not execute repository code in the credential holder (ADR 0007).

    ``DATABASE_URL`` stands in for the control container's secrets: the harness container does
    not pass it to the command, so a crafted ``Makefile`` cannot read it.
    """
    shared = tmp_path / "shared"
    worktree = _worktree(tmp_path)
    harness = CommandRunner(command=["python", "-c", "print('ok')"])
    thread = _serve_in_background(shared, harness, once=False, poll=0.02, idle_timeout=30)

    session = SidecarSession(shared, timeout=10, poll=0.02)
    verifier = CommandVerifier.from_env({}, sandbox=None)
    verifier = CommandVerifier(
        allowed=(("python", "-c", "print(1)"),),
        sandbox=verifier.sandbox,
        _execute=session.run_command,
    )
    os.environ["DATABASE_URL"] = "postgresql://control-only:secret@db/dear"
    try:
        result = verifier.run("python -c print(1)", str(worktree.path))
    finally:
        os.environ.pop("DATABASE_URL", None)
    session.close()
    thread.join(timeout=10)

    assert result.ok is True
    assert "1" in result.output
    # The command ran through the sidecar (the shared directory), not in this process.
    assert not (shared / COMMAND_REQUEST_FILE).exists()


def test_a_stale_result_is_ignored(tmp_path: Path) -> None:
    """A result tagged with another sequence number is not mistaken for the current one."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / RESULT_FILE).write_text(json.dumps({"exit_code": 0, "stdout": "stale", "seq": 99}))

    session = SidecarSession(shared, timeout=0.3, poll=0.05)
    DelegatingRunner(session).run(
        Task(id="e1", transport_id="<m1@x>"),
        TaskSpec(repo_url="", instructions="x"),
        _worktree(tmp_path),
    )

    # The stale answer was dropped and the request timed out instead of returning it.
    assert not (shared / RESULT_FILE).exists()
    assert (shared / REQUEST_FILE).exists()


def test_the_harness_exits_when_the_control_writes_shutdown(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    harness = CommandRunner(command=["python", "-c", "print('ran')"])
    thread = _serve_in_background(shared, harness, once=False, poll=0.02, idle_timeout=30)
    time.sleep(0.1)

    SidecarSession(shared).close()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert (shared / SHUTDOWN_FILE).exists()


def test_the_harness_exits_after_an_idle_timeout(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    thread = _serve_in_background(
        shared, CommandRunner(command=["true"]), once=False, poll=0.02, idle_timeout=0.2
    )

    thread.join(timeout=5)

    assert not thread.is_alive()


def test_requests_and_results_are_written_atomically(tmp_path: Path) -> None:
    """No partial file is ever visible: writes go through a temporary file and a rename."""
    shared = tmp_path / "shared"
    thread = _serve_in_background(
        shared, CommandRunner(command=["python", "-c", "print('x')"]), once=False, poll=0.02
    )
    session = SidecarSession(shared, timeout=5, poll=0.01)
    DelegatingRunner(session).run(
        Task(id="e1", transport_id="<m1@x>"),
        TaskSpec(repo_url="", instructions="x"),
        _worktree(tmp_path),
    )
    session.close()
    thread.join(timeout=10)

    leftovers = [p.name for p in shared.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert not (shared / REQUEST_FILE).exists()

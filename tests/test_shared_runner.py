from __future__ import annotations

import json
import threading
from pathlib import Path

from dear_agent.queue.models import Task, TaskSpec
from dear_agent.runners.command import CommandRunner
from dear_agent.runners.shared import DelegatingRunner, serve
from dear_agent.runners.worktree import Worktree


def _worktree(tmp_path: Path) -> Worktree:
    workdir = tmp_path / "wt"
    workdir.mkdir()
    return Worktree(repo_path=workdir, path=workdir, branch="dear-agent/x")


def test_delegating_runner_round_trips(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    harness = CommandRunner(command=["python", "-c", "print('harness ran')"])
    thread = threading.Thread(
        target=serve, args=(shared,), kwargs={"runner": harness, "once": True}, daemon=True
    )
    thread.start()

    result = DelegatingRunner(shared, timeout=10).run(
        Task(id="e1", transport_id="<m1@x>"),
        TaskSpec(repo_url="", instructions="do it"),
        _worktree(tmp_path),
    )
    thread.join(timeout=10)

    assert result.exit_code == 0
    assert "harness ran" in result.stdout


def test_delegating_runner_times_out_when_nothing_answers(tmp_path: Path) -> None:
    result = DelegatingRunner(tmp_path / "shared", timeout=0.4, poll=0.05).run(
        Task(id="e1", transport_id="<m1@x>"),
        TaskSpec(repo_url="", instructions="x"),
        _worktree(tmp_path),
    )

    assert result.exit_code == 124


def test_the_request_carries_no_credentials(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    # No server: the run times out and leaves the request file for inspection.
    DelegatingRunner(shared, timeout=0.2, poll=0.05).run(
        Task(id="e1", transport_id="<m1@x>"),
        TaskSpec(repo_url="", instructions="do it", model_request="local:x"),
        _worktree(tmp_path),
    )

    text = (shared / "harness-request.json").read_text()
    for forbidden in ("DATABASE_URL", "SSH", "TOKEN", "PASSWORD", "SECRET"):
        assert forbidden not in text
    assert set(json.loads(text)) == {
        "task_id",
        "instructions",
        "model_request",
        "workdir",
        "branch",
    }

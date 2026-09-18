from __future__ import annotations

import subprocess
from pathlib import Path

from herald.queue.models import Task, TaskSpec
from herald.runners.opencode import OpenCodeRunner
from herald.runners.port import Runner
from herald.runners.worktree import Worktree


def make_task() -> Task:
    return Task(id="e1", transport_id="<m1@x>", subject="add healthz")


def make_spec(instructions: str = "fix the build") -> TaskSpec:
    return TaskSpec(repo_url="https://example.com/o/r", instructions=instructions)


def make_worktree(tmp_path: Path) -> Worktree:
    path = tmp_path / "wt"
    path.mkdir()
    return Worktree(repo_path=tmp_path, path=path, branch="herald/s")


def completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_opencode_runner_satisfies_the_port() -> None:
    assert isinstance(OpenCodeRunner(), Runner)


def test_build_argv_passes_the_prompt_as_one_argument() -> None:
    argv = OpenCodeRunner().build_argv(make_spec("fix; rm -rf /"))

    assert argv[:2] == ["opencode", "run"]
    assert argv[-1] == "fix; rm -rf /"
    assert "sh" not in argv and "-c" not in argv


def test_build_argv_includes_the_model_when_configured() -> None:
    argv = OpenCodeRunner(model="local:qwen").build_argv(make_spec())

    assert argv == ["opencode", "run", "--model", "local:qwen", "fix the build"]


def test_run_executes_in_the_worktree(tmp_path: Path) -> None:
    worktree = make_worktree(tmp_path)
    calls: list[tuple[list[str], str, int]] = []

    def fake_execute(argv: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
        calls.append((argv, cwd, timeout))
        return completed(0, stdout="ok")

    runner = OpenCodeRunner(_execute=fake_execute)
    result = runner.run(make_task(), make_spec(), worktree)

    assert result.ok
    assert result.stdout == "ok"
    assert result.branch == "herald/s"
    assert calls[0][1] == str(worktree.path)
    assert calls[0][0] == ["opencode", "run", "fix the build"]


def test_run_reports_a_nonzero_exit(tmp_path: Path) -> None:
    runner = OpenCodeRunner(_execute=lambda argv, cwd, timeout: completed(2, stderr="boom"))

    result = runner.run(make_task(), make_spec(), make_worktree(tmp_path))

    assert result.exit_code == 2
    assert result.stderr == "boom"


def test_run_reports_a_missing_binary(tmp_path: Path) -> None:
    def missing(argv: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
        raise FileNotFoundError(argv[0])

    runner = OpenCodeRunner(binary="nope", _execute=missing)
    result = runner.run(make_task(), make_spec(), make_worktree(tmp_path))

    assert result.exit_code == 127
    assert "nope" in result.stderr


def test_run_reports_a_timeout(tmp_path: Path) -> None:
    def slow(argv: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout, output="partial")

    runner = OpenCodeRunner(_execute=slow)
    result = runner.run(make_task(), make_spec(), make_worktree(tmp_path))

    assert result.exit_code == 124
    assert result.stdout == "partial"

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dear_agent.queue.models import Task, TaskSpec
from dear_agent.runners.claude import ClaudeCodeRunner
from dear_agent.runners.codex import CodexRunner
from dear_agent.runners.harness import HarnessRunner
from dear_agent.runners.opencode import OpenCodeRunner
from dear_agent.runners.port import Runner
from dear_agent.runners.worktree import Worktree

RUNNERS: list[HarnessRunner] = [OpenCodeRunner(), ClaudeCodeRunner(), CodexRunner()]


def make_spec(instructions: str = "fix the build") -> TaskSpec:
    return TaskSpec(repo_url="https://example.com/o/r", instructions=instructions)


def make_task() -> Task:
    return Task(id="e1", transport_id="<m1@x>")


def make_worktree(tmp_path: Path) -> Worktree:
    path = tmp_path / "wt"
    path.mkdir()
    return Worktree(repo_path=tmp_path, path=path, branch="dear-agent/s")


def test_all_harnesses_satisfy_the_port() -> None:
    assert all(isinstance(runner, Runner) for runner in RUNNERS)


@pytest.mark.parametrize("runner", RUNNERS, ids=lambda r: type(r).__name__)
def test_prompt_is_a_single_argv_element_never_a_shell(runner: HarnessRunner) -> None:
    argv = runner.build_argv(make_spec("fix; rm -rf /"))

    assert argv[-1] == "fix; rm -rf /"
    assert "sh" not in argv
    assert "-c" not in argv


@pytest.mark.parametrize("runner", RUNNERS, ids=lambda r: type(r).__name__)
def test_binary_is_the_first_argv_element(runner: HarnessRunner) -> None:
    assert runner.build_argv(make_spec())[0] == runner.binary


def test_claude_uses_print_mode() -> None:
    assert ClaudeCodeRunner().build_argv(make_spec())[:2] == ["claude", "-p"]


def test_codex_uses_exec() -> None:
    assert CodexRunner().build_argv(make_spec())[:2] == ["codex", "exec"]


@pytest.mark.parametrize("runner", RUNNERS, ids=lambda r: type(r).__name__)
def test_model_flag_is_optional(runner: HarnessRunner) -> None:
    runner.model = None
    assert "--model" not in runner.build_argv(make_spec())

    runner.model = "some-model"
    assert "--model" in runner.build_argv(make_spec())


def test_run_executes_in_the_worktree(tmp_path: Path) -> None:
    worktree = make_worktree(tmp_path)
    seen: list[str] = []

    def fake_execute(argv: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
        seen.append(cwd)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="ok", stderr="")

    runner = ClaudeCodeRunner(_execute=fake_execute)
    result = runner.run(make_task(), make_spec(), worktree)

    assert result.ok
    assert seen == [str(worktree.path)]


class RecordingSandbox:
    available = True

    def wrap(self, argv: list[str], *, worktree: Path) -> list[str]:
        return ["bwrap", "--unshare-net", "--", *argv]


def test_sandbox_wraps_the_command(tmp_path: Path) -> None:
    worktree = make_worktree(tmp_path)
    seen: list[list[str]] = []

    def fake_execute(argv: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
        seen.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    runner = OpenCodeRunner(sandbox=RecordingSandbox(), _execute=fake_execute)
    runner.run(make_task(), make_spec(), worktree)

    assert seen[0][0] == "bwrap"
    assert "--unshare-net" in seen[0]
    assert "opencode" in seen[0]


def test_no_sandbox_leaves_the_command_unwrapped(tmp_path: Path) -> None:
    worktree = make_worktree(tmp_path)
    seen: list[list[str]] = []

    def fake_execute(argv: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
        seen.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    OpenCodeRunner(_execute=fake_execute).run(make_task(), make_spec(), worktree)

    assert seen[0][0] == "opencode"


def test_command_runner_satisfies_the_port() -> None:
    from dear_agent.runners.command import CommandRunner

    assert isinstance(CommandRunner(command=["my-harness"]), Runner)


def test_command_runner_uses_the_sandbox(tmp_path: Path) -> None:
    from dear_agent.runners.command import CommandRunner

    worktree = make_worktree(tmp_path)
    seen: list[list[str]] = []

    def fake_execute(argv: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
        seen.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    runner = CommandRunner(
        command=["my-harness", "--flag"], sandbox=RecordingSandbox(), _execute=fake_execute
    )
    runner.run(make_task(), make_spec(), worktree)

    assert seen[0][0] == "bwrap"
    assert "my-harness" in seen[0]
    assert "--flag" in seen[0]

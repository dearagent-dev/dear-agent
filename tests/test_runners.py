from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from herald.queue.models import Task, TaskSpec
from herald.runners.command import CommandRunner
from herald.runners.port import Runner
from herald.runners.worktree import Worktree, WorktreeError


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "source"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "initial")
    return path


def make_task() -> Task:
    return Task(id="e1", transport_id="<m1@x>", subject="add healthz")


def make_spec() -> TaskSpec:
    return TaskSpec(repo_url="https://example.com/o/r", instructions="fix the build")


def test_worktree_is_created_on_a_herald_branch(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree.create(repo, tmp_path / "wt", slug="add-healthz", base_branch="main")

    assert worktree.branch == "herald/add-healthz"
    assert worktree.path.exists()
    assert (worktree.path / "README.md").exists()


def test_worktree_does_not_touch_the_main_checkout(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree.create(repo, tmp_path / "wt", slug="s", base_branch="main")
    (worktree.path / "new.txt").write_text("work\n")

    assert not (repo / "new.txt").exists()


def test_worktree_remove_cleans_up(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree.create(repo, tmp_path / "wt", slug="s", base_branch="main")
    path = worktree.path
    assert path.exists()

    worktree.remove()

    assert not path.exists()


def test_worktree_context_manager_removes_on_exit(repo: Path, tmp_path: Path) -> None:
    with Worktree.create(repo, tmp_path / "wt", slug="s", base_branch="main") as worktree:
        path = worktree.path
        assert path.exists()

    assert not path.exists()


def test_worktree_create_fails_outside_a_repo(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    with pytest.raises(WorktreeError):
        Worktree.create(not_a_repo, tmp_path / "wt", slug="s")


def test_command_runner_captures_output(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree.create(repo, tmp_path / "wt", slug="s", base_branch="main")
    runner = CommandRunner(command=["sh", "-c", "echo ran > out.txt && echo done"])
    try:
        result = runner.run(make_task(), make_spec(), worktree)
    finally:
        worktree.remove()

    assert result.ok
    assert "done" in result.stdout
    assert result.branch == "herald/s"


def test_command_runner_reports_a_nonzero_exit(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree.create(repo, tmp_path / "wt", slug="s", base_branch="main")
    runner = CommandRunner(command=["sh", "-c", "exit 3"])
    try:
        result = runner.run(make_task(), make_spec(), worktree)
    finally:
        worktree.remove()

    assert result.exit_code == 3
    assert result.ok is False


def test_command_runner_reports_a_missing_binary(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree.create(repo, tmp_path / "wt", slug="s", base_branch="main")
    runner = CommandRunner(command=["definitely-not-a-real-binary-xyz"])
    try:
        result = runner.run(make_task(), make_spec(), worktree)
    finally:
        worktree.remove()

    assert result.exit_code == 127


def test_command_runner_times_out(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree.create(repo, tmp_path / "wt", slug="s", base_branch="main")
    runner = CommandRunner(command=["sleep", "5"], timeout_seconds=1)
    try:
        result = runner.run(make_task(), make_spec(), worktree)
    finally:
        worktree.remove()

    assert result.exit_code == 124


def test_command_runner_satisfies_the_port() -> None:
    assert isinstance(CommandRunner(command=["true"]), Runner)


def test_worktree_can_be_recreated_for_the_same_slug(repo: Path, tmp_path: Path) -> None:
    # A failed attempt leaves the branch behind; a retry must start from a clean tree.
    first = Worktree.create(repo, tmp_path / "wt", slug="s", base_branch="main")
    first.remove()

    second = Worktree.create(repo, tmp_path / "wt", slug="s", base_branch="main")
    try:
        assert second.branch == "herald/s"
    finally:
        second.remove()


def test_harness_env_points_pwd_at_the_worktree(tmp_path: Path) -> None:
    # OpenCode (and shell-based tools) resolve the project from $PWD, so it must be the
    # worktree, never Herald's own working directory.
    from herald.runners.harness import harness_env

    assert harness_env(str(tmp_path))["PWD"] == str(tmp_path)


def test_default_execute_passes_pwd_to_the_child(tmp_path: Path) -> None:
    from herald.runners.harness import default_execute

    result = default_execute(["sh", "-c", 'printf %s "$PWD"'], str(tmp_path), 10)

    assert result.stdout == str(tmp_path)


def test_command_runner_runs_with_the_worktree_as_pwd(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree.create(repo, tmp_path / "wt", slug="s", base_branch="main")
    runner = CommandRunner(command=["sh", "-c", 'printf %s "$PWD" > where.txt'])
    try:
        runner.run(make_task(), make_spec(), worktree)
        seen = (worktree.path / "where.txt").read_text()
    finally:
        worktree.remove()

    assert seen == str(worktree.path)

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dear_agent.gitplane.plane import GitPlane, ProtectedBranchError, PullRequest
from dear_agent.runners.worktree import Worktree


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


class RecordingForge:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def open_draft_pr(
        self, *, repo_path: Path, branch: str, base_branch: str, title: str, body: str
    ) -> PullRequest:
        self.calls.append(
            {
                "repo_path": repo_path,
                "branch": branch,
                "base_branch": base_branch,
                "title": title,
                "body": body,
            }
        )
        return PullRequest(url=f"https://example.com/pr/{len(self.calls)}", number=len(self.calls))


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


@pytest.fixture()
def worktree(repo: Path, tmp_path: Path) -> Worktree:
    return Worktree.create(repo, tmp_path / "wt", slug="add-healthz", base_branch="main")


def test_commit_all_commits_and_returns_the_sha(worktree: Worktree) -> None:
    plane = GitPlane(forge=RecordingForge())
    (worktree.path / "health.txt").write_text("ok\n")

    sha = plane.commit_all(worktree, message="add health endpoint")

    assert len(sha) == 40
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=worktree.path, capture_output=True, text=True
    )
    assert "add health endpoint" in log.stdout


def test_commit_all_is_a_noop_without_changes(worktree: Worktree) -> None:
    plane = GitPlane(forge=RecordingForge())

    sha = plane.commit_all(worktree, message="nothing")

    assert len(sha) == 40


def test_commit_refuses_a_protected_branch(repo: Path, tmp_path: Path) -> None:
    protected = Worktree(repo_path=repo, path=repo, branch="main")
    plane = GitPlane(forge=RecordingForge())

    with pytest.raises(ProtectedBranchError):
        plane.commit_all(protected, message="nope")


def test_push_refuses_a_protected_branch(repo: Path) -> None:
    protected = Worktree(repo_path=repo, path=repo, branch="main")

    with pytest.raises(ProtectedBranchError):
        GitPlane(forge=RecordingForge()).push(protected)


def test_open_draft_pr_delegates_to_the_forge(worktree: Worktree) -> None:
    forge = RecordingForge()
    plane = GitPlane(forge=forge)

    pr = plane.open_draft_pr(worktree, base_branch="main", title="add healthz", body="see branch")

    assert pr.is_draft
    assert forge.calls[0]["branch"] == "dear-agent/add-healthz"
    assert forge.calls[0]["base_branch"] == "main"


def test_open_draft_pr_refuses_a_protected_branch(repo: Path) -> None:
    protected = Worktree(repo_path=repo, path=repo, branch="main")

    with pytest.raises(ProtectedBranchError):
        GitPlane(forge=RecordingForge()).open_draft_pr(
            protected, base_branch="main", title="t", body="b"
        )


def test_push_env_pins_the_write_key() -> None:
    plane = GitPlane(forge=RecordingForge(), push_key="/run/secrets/git-push/ssh-privatekey")

    env = plane._push_env()

    assert env is not None
    assert "ssh -i /run/secrets/git-push/ssh-privatekey" in env["GIT_SSH_COMMAND"]
    assert "IdentitiesOnly=yes" in env["GIT_SSH_COMMAND"]


def test_push_env_is_unset_without_a_push_key() -> None:
    assert GitPlane(forge=RecordingForge())._push_env() is None

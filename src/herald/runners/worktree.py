from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class RunnerError(Exception):
    """Base class for runner errors."""


class WorktreeError(RunnerError):
    """Creating or removing a worktree failed."""


@dataclass(slots=True)
class Worktree:
    """An isolated git worktree.

    Created from a repository's base branch on a fresh ``herald/<slug>`` branch. The
    caller's working tree is never touched: this is a separate directory that can be
    deleted when the run ends.
    """

    repo_path: Path
    path: Path
    branch: str

    @classmethod
    def create(
        cls,
        repo_path: str | Path,
        worktrees_root: str | Path,
        *,
        slug: str,
        base_branch: str = "main",
    ) -> Worktree:
        repo = Path(repo_path).resolve()
        if not (repo / ".git").exists():
            raise WorktreeError(f"{repo} is not a git repository")

        branch = slug if slug.startswith("herald/") else f"herald/{slug}"
        root = Path(worktrees_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = root / slug

        # ``-B`` (not ``-b``) resets the branch to the base if a previous attempt left it
        # behind, so a requeued/retried task starts from a clean tree instead of failing.
        _git(repo, "worktree", "add", "-B", branch, str(path), base_branch)
        return cls(repo_path=repo, path=path, branch=branch)

    def remove(self) -> None:
        """Remove the worktree. Never raises if it is already gone."""
        if not self.path.exists():
            return
        _git(self.repo_path, "worktree", "remove", "--force", str(self.path))
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> Worktree:
        return self

    def __exit__(self, *exc: object) -> None:
        self.remove()


@dataclass(slots=True)
class RunResult:
    """What a harness produced. Evidence only; the PR is the deliverable."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    branch: str | None = None
    commit: str | None = None
    pr_url: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result

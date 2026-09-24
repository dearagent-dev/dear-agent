from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from dear_agent.runners.worktree import Worktree


class GitError(RuntimeError):
    """A git operation in the plane failed."""


class ProtectedBranchError(GitError):
    """An attempt was made to commit or push to a protected base branch."""


@dataclass(slots=True)
class PullRequest:
    """A draft PR, the deliverable of a task."""

    url: str
    number: int | None = None
    is_draft: bool = True


@runtime_checkable
class Forge(Protocol):
    """A code forge that can open a draft PR (GitHub, Gitea, ...)."""

    def open_draft_pr(
        self,
        *,
        repo_path: Path,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PullRequest: ...


@dataclass(slots=True)
class GitPlane:
    """Owns worktree commits, branch pushing and draft-PR creation.

    The agent never writes a protected base branch: committing or pushing to one raises
    :class:`ProtectedBranchError`. Only ``dear-agent/<slug>`` branches leave the worktree.
    """

    forge: Forge
    protected_branches: frozenset[str] = frozenset({"main", "master"})
    # A write deploy key used only for the push, so the clone can stay on the read key
    # (ADR 0003). Unset locally: the ambient git credential is used.
    push_key: str | None = None

    def commit_all(self, worktree: Worktree, *, message: str) -> str:
        self._assert_not_protected(worktree.branch)
        _git(worktree.path, "add", "-A")
        status = _git(worktree.path, "status", "--porcelain").stdout.strip()
        if status:
            _git(worktree.path, "commit", "-m", message)
        return _git(worktree.path, "rev-parse", "HEAD").stdout.strip()

    def push(self, worktree: Worktree, *, remote: str = "origin") -> None:
        self._assert_not_protected(worktree.branch)
        _git(
            worktree.path,
            "push",
            "--set-upstream",
            remote,
            worktree.branch,
            env=self._push_env(),
        )

    def _push_env(self) -> dict[str, str] | None:
        """The environment for the push, pinned to the write key when one is configured."""
        if not self.push_key:
            return None
        command = f"ssh -i {self.push_key} -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes"
        return {**os.environ, "GIT_SSH_COMMAND": command}

    def has_changes_since(self, worktree: Worktree, base_branch: str) -> bool:
        """True when the worktree branch differs from ``base_branch``."""
        self._assert_not_protected(worktree.branch)
        result = subprocess.run(
            ["git", "diff", "--quiet", base_branch, "--"],
            cwd=worktree.path,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 1

    def open_draft_pr(
        self,
        worktree: Worktree,
        *,
        base_branch: str,
        title: str,
        body: str,
    ) -> PullRequest:
        self._assert_not_protected(worktree.branch)
        return self.forge.open_draft_pr(
            repo_path=worktree.repo_path,
            branch=worktree.branch,
            base_branch=base_branch,
            title=title,
            body=body,
        )

    def _assert_not_protected(self, branch: str) -> None:
        if branch in self.protected_branches:
            raise ProtectedBranchError(f"{branch!r} is a protected branch")


def _git(
    path: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result

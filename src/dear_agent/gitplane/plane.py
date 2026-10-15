from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from dear_agent.policy import split_repo
from dear_agent.runners.worktree import Worktree

# The committer identity for agent commits, set explicitly because a runner container has no
# git user.name/user.email (see GitPlane.commit_all).
COMMITTER_NAME = "Dear Agent"
COMMITTER_EMAIL = "dear-agent@localhost"

# A hooks directory that does not exist, so git finds no hook to run whatever the checkout's
# config says. ``--cap-drop ALL`` and the harness sharing the volume mean we cannot rely on the
# file permissions of ``.git/hooks``.
NO_HOOKS_PATH = "/nonexistent/dear-agent/hooks"

# Config in the *checkout* that makes git execute a program when the control container runs it.
# The harness shares the volume and can write this config, so it is neutralised before every
# control-side git command. Credentials are unaffected: the runner supplies them through the
# environment (``GIT_CONFIG_*``) or the operator's global config, neither of which the harness
# can write.
_EXECUTABLE_SECTIONS = (
    "filter",
    "include",
    "includeif",
    "alias",
    "credential",
    "url",
    "diff",
    "http",
)
_EXECUTABLE_CORE_KEYS = (
    "hookspath",
    "fsmonitor",
    "sshcommand",
    "attributesfile",
    "editor",
    "pager",
)


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
        self._harden(worktree)
        self._git(worktree, "add", "-A")
        status = self._git(worktree, "status", "--porcelain").stdout.strip()
        if status:
            # An explicit identity: the runner container has no git user.name/user.email, and
            # `git commit` fails without one (locally the operator's global config hid this).
            self._git(
                worktree,
                "-c",
                f"user.name={COMMITTER_NAME}",
                "-c",
                f"user.email={COMMITTER_EMAIL}",
                "commit",
                "-m",
                message,
            )
        return self._git(worktree, "rev-parse", "HEAD").stdout.strip()

    def push(self, worktree: Worktree, *, remote: str = "origin") -> None:
        self._assert_not_protected(worktree.branch)
        self._harden(worktree)
        self._assert_remote_allowed(worktree, remote)
        self._git(
            worktree,
            "push",
            "--set-upstream",
            remote,
            worktree.branch,
            env=self._push_env(),
        )

    def _git(
        self,
        worktree: Worktree,
        *args: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run git in the worktree with hooks disabled, whatever the checkout says."""
        return _git(
            worktree.path,
            "-c",
            f"core.hooksPath={NO_HOOKS_PATH}",
            "-c",
            "core.fsmonitor=false",
            *args,
            env=env,
        )

    def _harden(self, worktree: Worktree) -> None:
        """Drop the checkout config the harness could have written to execute code in control.

        ``core.hooksPath`` is also overridden per command via ``-c`` (which wins over the
        config), but a clean/smudge ``filter``, a ``core.fsmonitor``, a ``core.sshCommand`` or a
        ``diff.*.textconv`` driver would run *inside* git, so the keys are removed outright. The
        keys are removed one by one: most of them live in subsections (``filter.evil.clean``),
        which ``git config --remove-section`` cannot address by bare section name.
        """
        result = subprocess.run(
            ["git", "config", "--local", "--list", "--name-only"],
            cwd=worktree.path,
            capture_output=True,
            text=True,
            check=False,
        )
        for name in result.stdout.split():
            parts = name.lower().split(".", 1)
            section = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
            if section in _EXECUTABLE_SECTIONS or (
                section == "core" and rest.split(".")[0] in _EXECUTABLE_CORE_KEYS
            ):
                self._config(worktree, "--unset-all", name)

    def _config(self, worktree: Worktree, *args: str) -> None:
        subprocess.run(
            ["git", "config", "--local", *args],
            cwd=worktree.path,
            capture_output=True,
            text=True,
            check=False,
        )

    def _assert_remote_allowed(self, worktree: Worktree, remote: str) -> None:
        """Refuse to push somewhere the deployment did not approve.

        The harness can rewrite ``origin`` after the clone; pushing would then publish the branch
        to another repository (and hand the forge token to whatever host the PR step derives from
        it). The host of the effective remote is checked immediately before the push.
        """
        result = subprocess.run(
            ["git", "remote", "get-url", remote],
            cwd=worktree.path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(f"unknown remote {remote!r}")
        host, _slug = split_repo(result.stdout.strip())
        if host is not None and host not in _allowed_hosts():
            raise GitError(f"refusing to push to unapproved host {host!r}")

    def _push_env(self) -> dict[str, str] | None:
        """The environment for the push, pinned to the write key when one is configured."""
        if not self.push_key:
            return None
        command = f"ssh -i {self.push_key} -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes"
        return {**os.environ, "GIT_SSH_COMMAND": command}

    def has_changes_since(self, worktree: Worktree, base_branch: str) -> bool:
        """True when the worktree branch differs from ``base_branch``."""
        self._assert_not_protected(worktree.branch)
        self._harden(worktree)
        result = subprocess.run(
            ["git", "diff", "--quiet", base_branch, "--"],
            cwd=worktree.path,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 1

    def diff(self, worktree: Worktree, base_branch: str, *, limit: int = 20000) -> str:
        """The staged diff against ``base_branch``, for a reviewer (ADR 0010).

        Stages the worktree first so new (untracked) files are included; ``commit_all`` stages
        again, so this is safe to call before the publish.
        """
        self._assert_not_protected(worktree.branch)
        self._harden(worktree)
        self._git(worktree, "add", "-A")
        result = self._git(worktree, "diff", "--cached", base_branch, "--")
        return (result.stdout or "")[:limit]

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


def _allowed_hosts() -> frozenset[str]:
    """The Git hosts the deployment trusts (``DEAR_AGENT_GIT_HOST``, default ``github.com``)."""
    raw = os.environ.get("DEAR_AGENT_GIT_HOST") or "github.com"
    return frozenset(
        part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()
    ) or frozenset({"github.com"})


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

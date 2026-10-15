from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from dear_agent.policy import normalize_repo, split_repo


class RepoError(RuntimeError):
    """A repository could not be prepared for a run."""


@dataclass(slots=True)
class RepoPreparer:
    """Ensures a read-only checkout of a repository exists before a run.

    The runner starts with no working tree; this clones it once (using the mounted read
    key via ``GIT_SSH_COMMAND``) and then reuses it. Cloning is the only network operation,
    and it is read-only: pushing happens later through the publisher identity.

    The preparer is the last wall before the credential holder: the **host** of a
    message-supplied URL is checked against ``allowed_hosts`` (default: the deployment's
    ``DEAR_AGENT_GIT_HOST``) and a reused checkout must already point at the *same*
    repository. Both close paths where an inbound message could aim the control container's
    Git credential (or task B's instructions at task A's checkout) at another origin.
    """

    binary: str = "git"
    allowed_hosts: tuple[str, ...] | None = None

    def ensure(self, repo_path: str | Path, repo_url: str) -> Path:
        self._assert_allowed(repo_url)
        path = Path(repo_path)
        if (path / ".git").exists():
            self._assert_same_repo(path, repo_url)
            return path
        if path.exists() and any(path.iterdir()):
            raise RepoError(f"{path} exists but is not a git repository")
        path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [self.binary, "clone", "--depth", "1", repo_url, str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RepoError(result.stderr.strip() or f"cannot clone {repo_url!r}")
        return path

    def _hosts(self) -> frozenset[str]:
        if self.allowed_hosts is not None:
            return frozenset(host.lower() for host in self.allowed_hosts if host.strip())
        import os

        raw = os.environ.get("DEAR_AGENT_GIT_HOST") or "github.com"
        return frozenset(
            part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()
        ) or frozenset({"github.com"})

    def _assert_allowed(self, repo_url: str) -> None:
        """Refuse a URL whose host is not approved. A local path has no host to approve."""
        host, _slug = split_repo(repo_url)
        if host is not None and host not in self._hosts():
            raise RepoError(
                f"refusing to use {repo_url!r}: unapproved host {host!r} "
                f"(expected one of {', '.join(sorted(self._hosts()))})"
            )

    def _assert_same_repo(self, path: Path, repo_url: str) -> None:
        """Fail when the existing checkout belongs to another repository (finding: repo reuse)."""
        result = subprocess.run(
            [self.binary, "remote", "get-url", "origin"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RepoError(f"{path} has no 'origin' remote; refusing to reuse it for {repo_url!r}")
        existing = result.stdout.strip()
        if normalize_repo(existing) != normalize_repo(repo_url):
            raise RepoError(
                f"{path} is a checkout of {existing!r}, not of {repo_url!r}; "
                "refusing to run this task against another repository"
            )


__all__ = ["RepoError", "RepoPreparer"]

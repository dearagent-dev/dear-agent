from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class RepoError(RuntimeError):
    """A repository could not be prepared for a run."""


@dataclass(slots=True)
class RepoPreparer:
    """Ensures a read-only checkout of a repository exists before a run.

    The runner starts with no working tree; this clones it once (using the mounted read
    key via ``GIT_SSH_COMMAND``) and then reuses it. Cloning is the only network operation,
    and it is read-only: pushing happens later through the publisher identity.
    """

    binary: str = "git"

    def ensure(self, repo_path: str | Path, repo_url: str) -> Path:
        path = Path(repo_path)
        if (path / ".git").exists():
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


__all__ = ["RepoError", "RepoPreparer"]

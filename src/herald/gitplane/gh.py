from __future__ import annotations

import subprocess
from pathlib import Path

from herald.gitplane.plane import GitError, PullRequest


class GhForge:
    """A :class:`~herald.gitplane.plane.Forge` backed by the ``gh`` CLI.

    Uses ``gh pr create --draft`` so the deliverable is always a draft PR a human lands.
    """

    def open_draft_pr(
        self,
        *,
        repo_path: Path,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PullRequest:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--base",
                base_branch,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "gh pr create failed")
        url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        return PullRequest(url=url, is_draft=True)

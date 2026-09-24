from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dear_agent.gitplane.plane import Forge, GitError, PullRequest

DEFAULT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class Remote:
    """A git remote reduced to the forge host and the project path."""

    host: str
    path: str


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: str


HttpPost = Callable[[str, Mapping[str, str], str], HttpResponse]


def default_http_post(url: str, headers: Mapping[str, str], body: str) -> HttpResponse:
    request = urllib.request.Request(
        url, data=body.encode("utf-8"), headers=dict(headers), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            return HttpResponse(response.status, response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        raise GitError(f"cannot reach {url}: {exc.reason}") from exc


def parse_remote(url: str) -> Remote:
    """Parse ``git@host:owner/repo.git``, ``https://host/owner/repo.git`` and friends."""
    value = url.strip()
    if not value:
        raise GitError("empty remote URL")
    if "://" in value:
        parsed = urllib.parse.urlparse(value)
        host = parsed.hostname or ""
        path = parsed.path
    else:
        host_part, sep, path = value.partition(":")
        if not sep:
            raise GitError(f"cannot parse remote {value!r}")
        host = host_part.rsplit("@", 1)[-1]
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path:
        raise GitError(f"cannot parse remote {value!r}")
    return Remote(host=host, path=path)


def read_origin(repo_path: str | Path) -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise GitError(result.stderr.strip() or "no 'origin' remote to open a PR against")
    return result.stdout.strip()


def provider_for_host(host: str) -> str:
    lowered = host.lower()
    if "github" in lowered:
        return "github"
    if "gitlab" in lowered:
        return "gitlab"
    if any(marker in lowered for marker in ("gitea", "forgejo", "codeberg")):
        return "gitea"
    return "github"


def _snippet(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[:limit]


def _first_env(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _post(
    http_post: HttpPost,
    url: str,
    headers: Mapping[str, str],
    payload: str,
    provider: str,
    parse: Callable[[dict[str, object]], PullRequest],
) -> PullRequest:
    response = http_post(url, headers, payload)
    if response.status not in (200, 201):
        raise GitError(f"{provider} API returned {response.status}: {_snippet(response.body)}")
    try:
        data = json.loads(response.body or "{}")
    except json.JSONDecodeError as exc:
        raise GitError(f"{provider} API returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise GitError(f"{provider} API returned an unexpected response")
    return parse(data)


@dataclass(slots=True)
class GithubApiForge:
    """Opens a draft PR through the GitHub REST API (no ``gh`` CLI, no extra binary)."""

    token: str
    http_post: HttpPost = field(default=default_http_post, repr=False)

    def open_draft_pr(
        self, *, repo_path: Path, branch: str, base_branch: str, title: str, body: str
    ) -> PullRequest:
        remote = parse_remote(read_origin(repo_path))
        if remote.host.lower() == "github.com":
            api = "https://api.github.com"
        else:  # GitHub Enterprise Server
            api = f"https://{remote.host}/api/v3"
        payload = json.dumps(
            {"title": title, "head": branch, "base": base_branch, "body": body, "draft": True}
        )
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

        def parse(data: dict[str, object]) -> PullRequest:
            return PullRequest(url=str(data["html_url"]), number=data.get("number"), is_draft=True)

        return _post(
            self.http_post, f"{api}/repos/{remote.path}/pulls", headers, payload, "github", parse
        )


@dataclass(slots=True)
class GitlabApiForge:
    """Opens a draft MR through the GitLab REST API (``Draft:`` title prefix)."""

    token: str
    http_post: HttpPost = field(default=default_http_post, repr=False)

    def open_draft_pr(
        self, *, repo_path: Path, branch: str, base_branch: str, title: str, body: str
    ) -> PullRequest:
        remote = parse_remote(read_origin(repo_path))
        project = urllib.parse.quote(remote.path, safe="")
        payload = json.dumps(
            {
                "source_branch": branch,
                "target_branch": base_branch,
                "title": f"Draft: {title}",
                "description": body,
            }
        )
        headers = {"PRIVATE-TOKEN": self.token, "Content-Type": "application/json"}

        def parse(data: dict[str, object]) -> PullRequest:
            return PullRequest(url=str(data["web_url"]), number=data.get("iid"), is_draft=True)

        return _post(
            self.http_post,
            f"https://{remote.host}/api/v4/projects/{project}/merge_requests",
            headers,
            payload,
            "gitlab",
            parse,
        )


@dataclass(slots=True)
class GiteaApiForge:
    """Opens a draft PR through the Gitea/Forgejo REST API."""

    token: str
    http_post: HttpPost = field(default=default_http_post, repr=False)

    def open_draft_pr(
        self, *, repo_path: Path, branch: str, base_branch: str, title: str, body: str
    ) -> PullRequest:
        remote = parse_remote(read_origin(repo_path))
        payload = json.dumps(
            {"title": title, "head": branch, "base": base_branch, "body": body, "draft": True}
        )
        headers = {"Authorization": f"token {self.token}", "Content-Type": "application/json"}

        def parse(data: dict[str, object]) -> PullRequest:
            return PullRequest(url=str(data["html_url"]), number=data.get("number"), is_draft=True)

        return _post(
            self.http_post,
            f"https://{remote.host}/api/v1/repos/{remote.path}/pulls",
            headers,
            payload,
            "gitea",
            parse,
        )


@dataclass(slots=True)
class AutoForge:
    """Picks the API forge from the repository's remote host, per task."""

    env: Mapping[str, str]
    http_post: HttpPost = field(default=default_http_post, repr=False)

    def open_draft_pr(
        self, *, repo_path: Path, branch: str, base_branch: str, title: str, body: str
    ) -> PullRequest:
        remote = parse_remote(read_origin(repo_path))
        provider = provider_for_host(remote.host)
        cls, token_envs = _FORGES[provider]
        token = _first_env(self.env, token_envs)
        if token is None:
            raise GitError(f"no {provider} token; set one of {', '.join(token_envs)}")
        return cls(token, http_post=self.http_post).open_draft_pr(
            repo_path=repo_path, branch=branch, base_branch=base_branch, title=title, body=body
        )


_FORGES: dict[str, tuple[type, tuple[str, ...]]] = {
    "github": (GithubApiForge, ("GH_TOKEN", "GITHUB_TOKEN")),
    "gitlab": (GitlabApiForge, ("GITLAB_TOKEN",)),
    "gitea": (GiteaApiForge, ("GITEA_TOKEN",)),
}


def build_forge(
    env: Mapping[str, str] | None = None, *, http_post: HttpPost | None = None
) -> Forge:
    """Build the forge that opens the draft PR/MR.

    ``DEAR_AGENT_FORGE`` selects the mechanism: ``auto`` (default) picks the API forge from each
    repository's remote host; ``github``/``gitlab``/``gitea`` force one API forge; ``gh`` keeps
    the legacy ``gh`` CLI. No ``gh`` binary is needed for the API forges.
    """
    source = env if env is not None else os.environ
    post = http_post or default_http_post
    choice = (source.get("DEAR_AGENT_FORGE") or "auto").strip().lower()
    if choice in ("gh", "cli"):
        from dear_agent.gitplane.gh import GhForge

        return GhForge()
    if choice == "auto":
        return AutoForge(source, http_post=post)
    selected = _FORGES.get(choice)
    if selected is None:
        raise GitError(f"unknown DEAR_AGENT_FORGE {choice!r}; expected auto|github|gitlab|gitea|gh")
    cls, token_envs = selected
    token = _first_env(source, token_envs)
    if token is None:
        raise GitError(f"DEAR_AGENT_FORGE={choice} needs one of {', '.join(token_envs)}")
    return cls(token, http_post=post)


__all__ = [
    "AutoForge",
    "GiteaApiForge",
    "GitlabApiForge",
    "GithubApiForge",
    "HttpPost",
    "HttpResponse",
    "Remote",
    "build_forge",
    "default_http_post",
    "parse_remote",
    "provider_for_host",
    "read_origin",
]

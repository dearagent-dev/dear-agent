from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from dear_agent.gitplane.forge import (
    AutoForge,
    BitbucketApiForge,
    GiteaApiForge,
    GithubApiForge,
    GitlabApiForge,
    HttpResponse,
    Remote,
    build_forge,
    parse_remote,
    provider_for_host,
)
from dear_agent.gitplane.gh import GhForge
from dear_agent.gitplane.plane import Forge, GitError


class Recorder:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, Mapping[str, str], str]] = []

    def __call__(self, url: str, headers: Mapping[str, str], body: str) -> HttpResponse:
        self.calls.append((url, headers, body))
        return self.response


def created(payload: dict[str, object]) -> HttpResponse:
    return HttpResponse(status=201, body=json.dumps(payload))


def origin(monkeypatch, url: str) -> None:
    monkeypatch.setattr("dear_agent.gitplane.forge.read_origin", lambda repo_path: url)


def test_parse_remote_scp_https_and_ssh_with_port() -> None:
    assert parse_remote("git@github.com:dearagent-dev/dear-agent.git") == Remote(
        "github.com", "dearagent-dev/dear-agent"
    )
    assert parse_remote("https://github.com/o/r.git") == Remote("github.com", "o/r")
    assert parse_remote("ssh://git@gitlab.example.com:2222/group/sub/repo.git") == Remote(
        "gitlab.example.com", "group/sub/repo"
    )
    assert parse_remote("https://gitlab.com/group/sub/repo") == Remote(
        "gitlab.com", "group/sub/repo"
    )


def test_provider_for_host() -> None:
    assert provider_for_host("github.com") == "github"
    assert provider_for_host("gitlab.com") == "gitlab"
    assert provider_for_host("codeberg.org") == "gitea"
    assert provider_for_host("git.example.internal") == "github"  # default


def test_build_forge_selects_by_configuration() -> None:
    assert isinstance(build_forge({}), AutoForge)
    assert isinstance(build_forge({"DEAR_AGENT_FORGE": "github", "GH_TOKEN": "t"}), GithubApiForge)
    assert isinstance(
        build_forge({"DEAR_AGENT_FORGE": "gitlab", "GITLAB_TOKEN": "t"}), GitlabApiForge
    )
    assert isinstance(build_forge({"DEAR_AGENT_FORGE": "gh"}), GhForge)


def test_build_forge_without_a_token_fails_loudly() -> None:
    with pytest.raises(GitError, match="GITHUB_TOKEN"):
        build_forge({"DEAR_AGENT_FORGE": "github"})


def test_build_forge_rejects_an_unknown_choice() -> None:
    with pytest.raises(GitError, match="unknown DEAR_AGENT_FORGE"):
        build_forge({"DEAR_AGENT_FORGE": "sourcehut"})


def test_github_forge_posts_a_draft_pr_and_reads_html_url(monkeypatch) -> None:
    origin(monkeypatch, "git@github.com:o/r.git")
    http = Recorder(created({"html_url": "https://github.com/o/r/pull/7", "number": 7}))
    forge = GithubApiForge("secret", http_post=http)

    pr = forge.open_draft_pr(
        repo_path=Path("/tmp/x"), branch="dear-agent/a", base_branch="main", title="t", body="b"
    )

    url, headers, body = http.calls[0]
    assert url == "https://api.github.com/repos/o/r/pulls"
    assert headers["Authorization"] == "Bearer secret"
    payload = json.loads(body)
    assert payload["draft"] is True
    assert payload["head"] == "dear-agent/a"
    assert payload["base"] == "main"
    assert pr.url == "https://github.com/o/r/pull/7"
    assert pr.number == 7
    assert pr.is_draft


def test_github_enterprise_uses_the_api_v3_base(monkeypatch) -> None:
    origin(monkeypatch, "git@ghe.corp.example:o/r.git")
    http = Recorder(created({"html_url": "https://ghe.corp.example/o/r/pull/1"}))
    GithubApiForge("secret", http_post=http).open_draft_pr(
        repo_path=Path("/tmp/x"), branch="b", base_branch="main", title="t", body="b"
    )

    assert http.calls[0][0] == "https://ghe.corp.example/api/v3/repos/o/r/pulls"


def test_gitlab_forge_encodes_the_project_and_prefixes_draft(monkeypatch) -> None:
    origin(monkeypatch, "git@gitlab.com:group/sub/repo.git")
    http = Recorder(
        created({"web_url": "https://gitlab.com/group/sub/repo/-/merge_requests/3", "iid": 3})
    )
    forge = GitlabApiForge("secret", http_post=http)

    pr = forge.open_draft_pr(
        repo_path=Path("/tmp/x"),
        branch="dear-agent/a",
        base_branch="main",
        title="Fix it",
        body="b",
    )

    url, headers, body = http.calls[0]
    assert url == "https://gitlab.com/api/v4/projects/group%2Fsub%2Frepo/merge_requests"
    assert headers["PRIVATE-TOKEN"] == "secret"
    assert json.loads(body)["title"] == "Draft: Fix it"
    assert json.loads(body)["source_branch"] == "dear-agent/a"
    assert pr.url.endswith("/merge_requests/3")
    assert pr.number == 3


def test_gitea_forge_posts_a_draft_pr(monkeypatch) -> None:
    origin(monkeypatch, "ssh://git@codeberg.org/o/r.git")
    http = Recorder(created({"html_url": "https://codeberg.org/o/r/pulls/9", "number": 9}))
    GiteaApiForge("secret", http_post=http).open_draft_pr(
        repo_path=Path("/tmp/x"), branch="b", base_branch="main", title="t", body="b"
    )

    url, headers, _ = http.calls[0]
    assert url == "https://codeberg.org/api/v1/repos/o/r/pulls"
    assert headers["Authorization"] == "token secret"


def test_a_non_success_response_is_a_git_error(monkeypatch) -> None:
    origin(monkeypatch, "git@github.com:o/r.git")
    http = Recorder(HttpResponse(status=422, body='{"message":"Validation Failed"}'))
    forge = GithubApiForge("secret", http_post=http)

    with pytest.raises(GitError, match="github API returned 422"):
        forge.open_draft_pr(
            repo_path=Path("/tmp/x"), branch="b", base_branch="main", title="t", body="b"
        )


def test_auto_forge_picks_gitlab_from_the_remote(monkeypatch) -> None:
    origin(monkeypatch, "git@gitlab.com:group/repo.git")
    http = Recorder(created({"web_url": "https://gitlab.com/group/repo/-/merge_requests/1"}))
    forge = AutoForge({"GITLAB_TOKEN": "secret"}, http_post=http)

    pr = forge.open_draft_pr(
        repo_path=Path("/tmp/x"), branch="b", base_branch="main", title="t", body="b"
    )

    assert http.calls[0][0].startswith("https://gitlab.com/api/v4/")
    assert pr.is_draft
    assert isinstance(forge, Forge)


def test_auto_forge_without_a_token_fails_loudly(monkeypatch) -> None:
    origin(monkeypatch, "git@github.com:o/r.git")
    http = Recorder(created({}))

    with pytest.raises(GitError, match="GITHUB_TOKEN"):
        AutoForge({}, http_post=http).open_draft_pr(
            repo_path=Path("/tmp/x"), branch="b", base_branch="main", title="t", body="b"
        )


def test_bitbucket_host_maps_to_the_bitbucket_provider() -> None:
    assert provider_for_host("bitbucket.org") == "bitbucket"
    assert provider_for_host("git@bitbucket.org") == "bitbucket"


def test_build_forge_selects_bitbucket_with_basic_credentials() -> None:
    forge = build_forge(
        {
            "DEAR_AGENT_FORGE": "bitbucket",
            "BITBUCKET_USERNAME": "dev@example.com",
            "BITBUCKET_TOKEN": "token",
        }
    )

    assert isinstance(forge, BitbucketApiForge)


def test_build_forge_bitbucket_without_credentials_fails_loudly() -> None:
    with pytest.raises(GitError, match="BITBUCKET_TOKEN"):
        build_forge({"DEAR_AGENT_FORGE": "bitbucket", "BITBUCKET_TOKEN": "token"})


def test_bitbucket_forge_posts_a_draft_pr_with_basic_auth(monkeypatch) -> None:
    origin(monkeypatch, "git@bitbucket.org:my-workspace/my-repo.git")
    http = Recorder(
        created(
            {
                "id": 5,
                "links": {
                    "html": {"href": "https://bitbucket.org/my-workspace/my-repo/pull-requests/5"}
                },
            }
        )
    )
    forge = BitbucketApiForge("dev@example.com", "token", http_post=http)

    pr = forge.open_draft_pr(
        repo_path=Path("/tmp/x"), branch="dear-agent/a", base_branch="main", title="t", body="b"
    )

    url, headers, body = http.calls[0]
    assert url == "https://api.bitbucket.org/2.0/repositories/my-workspace/my-repo/pullrequests"
    expected = base64.b64encode(b"dev@example.com:token").decode()
    assert headers["Authorization"] == f"Basic {expected}"
    payload = json.loads(body)
    assert payload["draft"] is True
    assert payload["source"] == {"branch": {"name": "dear-agent/a"}}
    assert payload["destination"] == {"branch": {"name": "main"}}
    assert pr.url.endswith("/pull-requests/5")
    assert pr.number == 5
    assert pr.is_draft


def test_auto_forge_picks_bitbucket_from_the_remote(monkeypatch) -> None:
    origin(monkeypatch, "git@bitbucket.org:my-workspace/my-repo.git")
    http = Recorder(
        created({"links": {"html": {"href": "https://bitbucket.org/x/y/pull-requests/1"}}})
    )
    forge = AutoForge(
        {"BITBUCKET_USERNAME": "dev@example.com", "BITBUCKET_TOKEN": "token"}, http_post=http
    )

    pr = forge.open_draft_pr(
        repo_path=Path("/tmp/x"), branch="b", base_branch="main", title="t", body="b"
    )

    assert http.calls[0][0].startswith("https://api.bitbucket.org/")
    assert pr.is_draft

from __future__ import annotations

import os

import pytest

from dear_agent.policy import (
    MemoryPolicyStore,
    PolicyStore,
    ProjectPolicy,
    load_policies,
    normalize_repo,
    parse_policy,
    split_repo,
    sync_policies,
)

DSN = os.environ.get("DEAR_AGENT_TEST_DATABASE_URL")

TEXT = """---
project: lab
repos:
  - "acme/*"
enabled: false
base_branch: develop
verify_allow:
  - make test
---

# Lab
"""


def test_parse_policy_reads_the_front_matter() -> None:
    policy = parse_policy(TEXT)

    assert policy.project == "lab"
    assert policy.enabled is False
    assert policy.repos == ("acme/*",)
    assert policy.base_branch == "develop"
    assert policy.verify_allow == ("make test",)


def test_parse_policy_requires_a_project() -> None:
    with pytest.raises(ValueError):
        parse_policy("---\nrepos: [a/b]\n---\n")


def test_normalize_repo_handles_urls_and_slugs() -> None:
    assert normalize_repo("https://github.com/Acme/Widget.git") == "github.com/acme/widget"
    assert normalize_repo("git@github.com:Acme/Widget.git") == "github.com/acme/widget"
    assert normalize_repo("ssh://git@github.com:22/Acme/Widget.git") == "github.com/acme/widget"
    assert normalize_repo("Acme/Widget") == "acme/widget"


def test_split_repo_separates_the_host_from_the_slug() -> None:
    assert split_repo("https://github.com/acme/widget.git") == ("github.com", "acme/widget")
    assert split_repo("git@gitlab.com:acme/widget.git") == ("gitlab.com", "acme/widget")
    assert split_repo("acme/widget") == (None, "acme/widget")


def test_policy_matches_a_repo_by_glob() -> None:
    policy = ProjectPolicy(project="p", repos=("acme/*",))

    assert policy.matches("https://github.com/acme/widget") is True
    assert policy.matches("https://github.com/other/widget") is False


def test_policy_rejects_a_lookalike_host() -> None:
    """A URL that merely *contains* github.com must not match a hostless pattern.

    This is the allowlist bypass: ``normalize_repo`` used to split on the first
    ``github.com/`` anywhere in the string, so an attacker host embedding it in its path
    matched the policy while ``RepoPreparer`` cloned the attacker's repository.
    """
    policy = ProjectPolicy(project="p", repos=("acme/*",))

    assert policy.matches("https://evil.example/github.com/acme/widget.git") is False
    assert policy.matches("https://github.com.evil.example/acme/widget.git") is False
    assert policy.matches("https://evil.example/acme/widget.git") is False
    assert policy.matches("https://evil.example/acme/widget") is False
    assert policy.matches("evil.example/acme/widget") is False


def test_policy_rejects_url_userinfo_spoofing() -> None:
    """``https://github.com@evil.example/acme/widget`` is the host evil.example."""
    policy = ProjectPolicy(project="p", repos=("acme/*",))

    assert policy.matches("https://github.com@evil.example/acme/widget.git") is False


def test_policy_accepts_an_explicit_host_pattern() -> None:
    policy = ProjectPolicy(project="p", repos=("gitlab.com/acme/*",))

    assert policy.matches("https://gitlab.com/acme/widget") is True
    assert policy.matches("https://github.com/acme/widget") is False


def test_policy_honours_the_configured_hosts() -> None:
    policy = ProjectPolicy(project="p", repos=("acme/*",))

    assert policy.matches("https://gitlab.com/acme/widget", hosts=("gitlab.com",)) is True
    assert policy.matches("https://github.com/acme/widget", hosts=("gitlab.com",)) is False


def test_memory_store_satisfies_the_port() -> None:
    assert isinstance(MemoryPolicyStore(), PolicyStore)


def test_memory_store_replace_and_lookup() -> None:
    store = MemoryPolicyStore()
    store.replace_all([ProjectPolicy(project="p", repos=("acme/*",))])

    assert store.get("p") is not None
    assert store.for_repo("acme/widget").project == "p"  # type: ignore[union-attr]
    assert store.for_repo("other/widget") is None

    store.replace_all([])
    assert store.list() == []


def test_sync_projects_a_markdown_directory(tmp_path) -> None:
    (tmp_path / "lab.md").write_text(TEXT)
    store = MemoryPolicyStore()

    policies = sync_policies(tmp_path, store)

    assert [policy.project for policy in policies] == ["lab"]
    assert store.get("lab") is not None


def test_load_policies_requires_a_directory(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_policies(tmp_path / "missing")


@pytest.mark.skipif(not DSN, reason="DEAR_AGENT_TEST_DATABASE_URL is not set")
def test_postgres_policy_projection_round_trip() -> None:
    pytest.importorskip("psycopg")
    from dear_agent.db import connect, init_schema
    from dear_agent.policy import PostgresPolicyStore

    conn = connect(DSN)
    init_schema(conn)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE dear_agent_policy")
    conn.commit()
    store = PostgresPolicyStore(conn)
    try:
        store.replace_all(
            [
                ProjectPolicy(
                    project="lab",
                    enabled=False,
                    repos=("acme/*",),
                    verify_allow=("make test",),
                )
            ]
        )

        fetched = store.get("lab")

        assert fetched is not None
        assert fetched.enabled is False
        assert fetched.verify_allow == ("make test",)
        assert store.for_repo("https://github.com/acme/widget").project == "lab"  # type: ignore[union-attr]
    finally:
        conn.close()

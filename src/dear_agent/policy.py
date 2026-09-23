from __future__ import annotations

import fnmatch
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from dear_agent.queue.models import utcnow

POLICY_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS dear_agent_policy (
        project      text PRIMARY KEY,
        enabled      boolean NOT NULL DEFAULT true,
        repos        jsonb NOT NULL DEFAULT '[]'::jsonb,
        base_branch  text,
        harness      text,
        model        text,
        verify_allow jsonb NOT NULL DEFAULT '[]'::jsonb,
        source       text,
        updated_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
)


def apply_schema(conn: Any) -> None:
    """Create the policy table if it does not exist. Idempotent."""
    with conn.cursor() as cur:
        for statement in POLICY_SCHEMA_STATEMENTS:
            cur.execute(statement)
    conn.commit()


@dataclass(slots=True, frozen=True)
class ProjectPolicy:
    """Per-project policy, declared in markdown and projected into the database.

    The markdown is the editable source; the row is a projection. Only fields that *narrow*
    or *default* behavior live here — nothing that could widen what an untrusted message is
    allowed to do (e.g. the verify allowlist is still enforced on top of the operator's).
    """

    project: str
    enabled: bool = True
    repos: tuple[str, ...] = ()
    base_branch: str | None = None
    harness: str | None = None
    model: str | None = None
    verify_allow: tuple[str, ...] = ()
    source: str | None = None

    def matches(self, repo: str) -> bool:
        slug = normalize_repo(repo)
        return any(fnmatch.fnmatch(slug, normalize_repo(pattern)) for pattern in self.repos)


@runtime_checkable
class PolicyStore(Protocol):
    """Reads the projected policies. Implementations keep the markdown the source of truth."""

    def list(self) -> list[ProjectPolicy]: ...
    def get(self, project: str) -> ProjectPolicy | None: ...
    def replace_all(self, policies: list[ProjectPolicy]) -> None: ...
    def for_repo(self, repo: str) -> ProjectPolicy | None: ...


def normalize_repo(repo: str) -> str:
    """Reduce a repo URL or slug to ``owner/repo`` so patterns can be matched."""
    value = repo.strip().lower().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if "github.com/" in value:
        value = value.split("github.com/", 1)[1]
    elif value.startswith("git@") and ":" in value:
        value = value.split(":", 1)[1]
    return value


def parse_policy(text: str, *, source: str | None = None) -> ProjectPolicy:
    """Parse a policy markdown file (YAML front matter + prose)."""
    if not text.lstrip().startswith("---"):
        raise ValueError("policy markdown must start with a YAML front matter block")
    _, _, remainder = text.partition("---")
    front_matter, _, _ = remainder.partition("---")
    data = yaml.safe_load(front_matter) or {}
    if not isinstance(data, dict) or "project" not in data:
        raise ValueError("policy front matter needs a 'project'")
    return ProjectPolicy(
        project=str(data["project"]),
        enabled=bool(data.get("enabled", True)),
        repos=tuple(str(item) for item in (data.get("repos") or [])),
        base_branch=data.get("base_branch"),
        harness=data.get("harness"),
        model=data.get("model"),
        verify_allow=tuple(str(item) for item in (data.get("verify_allow") or [])),
        source=source,
    )


def load_policies(directory: str | Path) -> list[ProjectPolicy]:
    """Parse every ``*.md`` policy file in ``directory`` (sorted for stability)."""
    root = Path(directory)
    if not root.exists():
        raise FileNotFoundError(f"policy directory {root} does not exist")
    policies: list[ProjectPolicy] = []
    for path in sorted(root.glob("*.md")):
        policies.append(parse_policy(path.read_text(encoding="utf-8"), source=str(path)))
    return policies


def sync_policies(directory: str | Path, store: PolicyStore) -> list[ProjectPolicy]:
    """Project the markdown directory into the store, replacing whatever was there."""
    policies = load_policies(directory)
    store.replace_all(policies)
    return policies


def _for_repo(policies: Iterable[ProjectPolicy], repo: str) -> ProjectPolicy | None:
    for policy in policies:
        if policy.matches(repo):
            return policy
    return None


class MemoryPolicyStore:
    """In-memory policy store for tests and single-process runs."""

    def __init__(self, policies: list[ProjectPolicy] | None = None) -> None:
        self._policies = list(policies or [])

    def list(self) -> list[ProjectPolicy]:
        return list(self._policies)

    def get(self, project: str) -> ProjectPolicy | None:
        return next((policy for policy in self._policies if policy.project == project), None)

    def replace_all(self, policies: list[ProjectPolicy]) -> None:
        self._policies = list(policies)

    def for_repo(self, repo: str) -> ProjectPolicy | None:
        return _for_repo(self._policies, repo)


class PostgresPolicyStore:
    """Durable policy projection in PostgreSQL (ADR 0005)."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def list(self) -> list[ProjectPolicy]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT project, enabled, repos, base_branch, harness, model, verify_allow, source "
                "FROM dear_agent_policy ORDER BY project"
            )
            rows = cur.fetchall()
        self._conn.commit()
        return [_policy_from_row(row) for row in rows]

    def get(self, project: str) -> ProjectPolicy | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT project, enabled, repos, base_branch, harness, model, verify_allow, source "
                "FROM dear_agent_policy WHERE project = %s",
                (project,),
            )
            row = cur.fetchone()
        self._conn.commit()
        return _policy_from_row(row) if row else None

    def replace_all(self, policies: list[ProjectPolicy]) -> None:
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM dear_agent_policy")
            for policy in policies:
                cur.execute(
                    """
                    INSERT INTO dear_agent_policy
                        (project, enabled, repos, base_branch, harness, model, verify_allow,
                         source, updated_at)
                    VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s)
                    """,
                    (
                        policy.project,
                        policy.enabled,
                        json.dumps(list(policy.repos)),
                        policy.base_branch,
                        policy.harness,
                        policy.model,
                        json.dumps(list(policy.verify_allow)),
                        policy.source,
                        utcnow(),
                    ),
                )
        self._conn.commit()

    def for_repo(self, repo: str) -> ProjectPolicy | None:
        return _for_repo(self.list(), repo)


def _policy_from_row(row: tuple) -> ProjectPolicy:
    project, enabled, repos, base_branch, harness, model, verify_allow, source = row
    return ProjectPolicy(
        project=project,
        enabled=enabled,
        repos=tuple(repos or []),
        base_branch=base_branch,
        harness=harness,
        model=model,
        verify_allow=tuple(verify_allow or []),
        source=source,
    )


def build_policy_store() -> PolicyStore:
    """Build the durable policy projection when the queue is PostgreSQL, else in-memory."""
    import os

    if os.environ.get("DEAR_AGENT_QUEUE", "memory").strip().lower() == "postgres":
        from dear_agent.db import connect, init_schema

        dsn = os.environ.get("DEAR_AGENT_DATABASE_URL")
        if not dsn:
            raise RuntimeError("DEAR_AGENT_DATABASE_URL is required for the postgres queue")
        conn = connect(dsn)
        init_schema(conn)
        return PostgresPolicyStore(conn)
    return MemoryPolicyStore()


__all__ = [
    "POLICY_SCHEMA_STATEMENTS",
    "MemoryPolicyStore",
    "PolicyStore",
    "PostgresPolicyStore",
    "ProjectPolicy",
    "apply_schema",
    "build_policy_store",
    "load_policies",
    "normalize_repo",
    "parse_policy",
    "sync_policies",
]

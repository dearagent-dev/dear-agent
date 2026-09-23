from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dear_agent.idle.signals import SignalCollector, SignalKind


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "source"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "app.py").write_text("print('hi')\n")
    git(path, "add", "app.py")
    git(path, "commit", "-m", "add app")
    (path / "app.py").write_text("print('hi')\n# TODO: handle errors\n")
    git(path, "commit", "-am", "add health endpoint")
    return path


def test_collects_recent_commits(repo: Path) -> None:
    signals = SignalCollector().collect(repo)

    subjects = [signal.summary for signal in signals.of_kind(SignalKind.COMMIT)]
    assert "add app" in subjects
    assert "add health endpoint" in subjects


def test_commit_evidence_is_an_abbreviated_sha(repo: Path) -> None:
    signals = SignalCollector().collect(repo)

    evidence = signals.of_kind(SignalKind.COMMIT)[0].evidence
    assert 7 <= len(evidence) <= 12


def test_collects_todo_markers(repo: Path) -> None:
    signals = SignalCollector().collect(repo)

    todos = signals.of_kind(SignalKind.TODO)
    assert any("handle errors" in signal.summary for signal in todos)
    assert todos[0].evidence.endswith("app.py")


def test_collect_on_a_non_repo_is_empty(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    signals = SignalCollector().collect(plain)

    assert signals.items == []


def test_collect_respects_the_since_window(tmp_path: Path) -> None:
    path = tmp_path / "source"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "f.txt").write_text("x\n")
    git(path, "add", "f.txt")
    old_env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z",
    }
    subprocess.run(
        ["git", "commit", "-m", "old commit"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=old_env,
    )

    signals = SignalCollector().collect(path, now=datetime(2026, 1, 1, tzinfo=UTC))

    assert signals.of_kind(SignalKind.COMMIT) == []


def test_signals_expose_since(repo: Path) -> None:
    signals = SignalCollector().collect(repo, now=datetime(2026, 1, 1, tzinfo=UTC))

    assert signals.since < datetime(2026, 1, 1, tzinfo=UTC)

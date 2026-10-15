from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dear_agent.repo import RepoError, RepoPreparer


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "README.md").write_text("hi\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    return path


def test_ensure_clones_a_repo(tmp_path: Path) -> None:
    origin = _init_repo(tmp_path / "origin")
    dest = tmp_path / "work" / "source"

    result = RepoPreparer().ensure(dest, str(origin))

    assert (result / ".git").exists()
    assert (result / "README.md").read_text() == "hi\n"


def test_ensure_reuses_an_existing_checkout(tmp_path: Path) -> None:
    origin = _init_repo(tmp_path / "origin")
    dest = tmp_path / "source"
    preparer = RepoPreparer()

    preparer.ensure(dest, str(origin))
    before = (dest / ".git").stat().st_mtime
    preparer.ensure(dest, str(origin))

    assert (dest / ".git").stat().st_mtime == before


def test_ensure_refuses_a_non_git_directory(tmp_path: Path) -> None:
    dest = tmp_path / "source"
    dest.mkdir()
    (dest / "stray.txt").write_text("x\n")

    with pytest.raises(RepoError):
        RepoPreparer().ensure(dest, "https://example.com/o/r.git")


def test_ensure_surfaces_clone_failure(tmp_path: Path) -> None:
    with pytest.raises(RepoError):
        RepoPreparer().ensure(tmp_path / "source", str(tmp_path / "does-not-exist"))


def test_ensure_refuses_an_unapproved_host(tmp_path: Path) -> None:
    """A message-supplied URL must not reach a host the deployment did not approve.

    The policy matcher refuses the lookalike host, but the clone is the last wall: even with
    an open policy (``DEAR_AGENT_REQUIRE_REPO_POLICY=false``) the preparer must not clone from
    an arbitrary host, because the control container offers its forge credential to it.
    """
    with pytest.raises(RepoError):
        RepoPreparer(allowed_hosts=("github.com",)).ensure(
            tmp_path / "source", "https://evil.example/github.com/acme/widget.git"
        )


def test_ensure_allows_the_approved_host(tmp_path: Path) -> None:
    # The clone itself then fails (no network in the test), which is the point: the host passed.
    with pytest.raises(RepoError) as excinfo:
        RepoPreparer(allowed_hosts=("github.com",)).ensure(
            tmp_path / "source", "https://github.com/acme/widget.git"
        )
    assert "unapproved host" not in str(excinfo.value)


def test_ensure_allows_a_local_path(tmp_path: Path) -> None:
    origin = _init_repo(tmp_path / "origin")

    result = RepoPreparer(allowed_hosts=("github.com",)).ensure(tmp_path / "source", str(origin))

    assert (result / "README.md").read_text() == "hi\n"


def test_ensure_refuses_a_reused_checkout_for_another_repo(tmp_path: Path) -> None:
    """A reused ``--repo`` directory must not silently run task B against repo A."""
    origin = _init_repo(tmp_path / "origin")
    dest = tmp_path / "source"
    preparer = RepoPreparer()
    preparer.ensure(dest, str(origin))

    with pytest.raises(RepoError):
        preparer.ensure(dest, "https://github.com/other/widget.git")


def test_ensure_accepts_the_same_repo_by_another_url(tmp_path: Path) -> None:
    """``git@github.com:acme/widget`` and ``https://github.com/acme/widget`` are one repo."""
    origin = _init_repo(tmp_path / "origin")
    dest = tmp_path / "source"
    preparer = RepoPreparer()
    preparer.ensure(dest, str(origin))
    _git(dest, "remote", "set-url", "origin", "https://github.com/acme/widget.git")

    result = preparer.ensure(dest, "git@github.com:acme/widget.git")

    assert result == dest

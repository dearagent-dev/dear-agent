from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from herald.repo import RepoError, RepoPreparer


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

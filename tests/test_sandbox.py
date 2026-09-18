from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from herald.sandbox import (
    BubblewrapSandbox,
    NoSandbox,
    Sandbox,
    SandboxError,
    SandboxPolicy,
)


def test_no_sandbox_returns_the_command_unchanged() -> None:
    argv = ["opencode", "run", "hello"]

    wrapped = NoSandbox().wrap(argv, worktree=Path("/tmp/wt"))

    assert wrapped == argv
    assert NoSandbox().available is False


def test_no_sandbox_satisfies_the_port() -> None:
    assert isinstance(NoSandbox(), Sandbox)


def test_bubblewrap_denies_network_by_default() -> None:
    wrapped = BubblewrapSandbox().wrap(["true"], worktree=Path("/tmp/wt"))

    assert "--unshare-net" in wrapped


def test_bubblewrap_allows_network_when_policy_permits() -> None:
    sandbox = BubblewrapSandbox(policy=SandboxPolicy(allow_network=True))

    wrapped = sandbox.wrap(["true"], worktree=Path("/tmp/wt"))

    assert "--unshare-net" not in wrapped


def test_bubblewrap_binds_the_worktree_and_chdirs_into_it() -> None:
    wrapped = BubblewrapSandbox().wrap(["true"], worktree=Path("/tmp/wt"))

    assert "--bind" in wrapped
    assert "/tmp/wt" in wrapped
    assert wrapped[wrapped.index("--chdir") + 1] == "/tmp/wt"


def test_bubblewrap_clears_the_environment() -> None:
    wrapped = BubblewrapSandbox().wrap(["true"], worktree=Path("/tmp/wt"))

    assert "--clearenv" in wrapped


def test_bubblewrap_is_a_no_op_to_build_when_unavailable() -> None:
    sandbox = BubblewrapSandbox(binary="bwrap-does-not-exist")

    with pytest.raises(SandboxError):
        sandbox.wrap(["true"], worktree=Path("/tmp/wt"))


@pytest.mark.skipif(
    subprocess.run(["sh", "-c", "command -v bwrap"], capture_output=True).returncode != 0,
    reason="bubblewrap not installed",
)
def test_bubblewrap_actually_runs_a_command(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    sandbox = BubblewrapSandbox()

    wrapped = sandbox.wrap(["sh", "-c", "echo sandboxed > out.txt"], worktree=worktree)
    result = subprocess.run(wrapped, cwd=worktree, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert (worktree / "out.txt").read_text().strip() == "sandboxed"

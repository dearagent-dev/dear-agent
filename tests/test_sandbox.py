from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from herald.sandbox import (
    BubblewrapSandbox,
    NoSandbox,
    Sandbox,
    SandboxError,
    SandboxPolicy,
    sandbox_policy_from_env,
)

HAS_BWRAP = shutil.which("bwrap") is not None


def bwrap_argv(argv: list[str], **kwargs: object) -> list[str]:
    """Build a bwrap invocation regardless of whether bwrap is installed."""
    return BubblewrapSandbox(binary="bwrap").build_wrapped_argv(argv, worktree=Path("/tmp/wt"))


def test_no_sandbox_returns_the_command_unchanged() -> None:
    argv = ["opencode", "run", "hello"]

    wrapped = NoSandbox().wrap(argv, worktree=Path("/tmp/wt"))

    assert wrapped == argv
    assert NoSandbox().available is False


def test_no_sandbox_satisfies_the_port() -> None:
    assert isinstance(NoSandbox(), Sandbox)


def test_bubblewrap_denies_network_by_default() -> None:
    assert "--unshare-net" in bwrap_argv(["true"])


def test_bubblewrap_allows_network_when_policy_permits() -> None:
    sandbox = BubblewrapSandbox(binary="bwrap", policy=SandboxPolicy(allow_network=True))

    wrapped = sandbox.build_wrapped_argv(["true"], worktree=Path("/tmp/wt"))

    assert "--unshare-net" not in wrapped


def test_bubblewrap_binds_the_worktree_and_chdirs_into_it() -> None:
    wrapped = bwrap_argv(["true"])

    assert "--bind" in wrapped
    assert "/tmp/wt" in wrapped
    assert wrapped[wrapped.index("--chdir") + 1] == "/tmp/wt"


def test_bubblewrap_clears_the_environment() -> None:
    assert "--clearenv" in bwrap_argv(["true"])


def test_policy_from_env_defaults_to_no_network_and_no_home() -> None:
    policy = sandbox_policy_from_env({})

    assert policy.allow_network is False
    assert "/home" not in policy.readable_paths
    assert "PATH" in policy.env_allowlist


def test_policy_from_env_can_allow_network_and_extend_paths_and_env() -> None:
    policy = sandbox_policy_from_env(
        {
            "HERALD_SANDBOX_NETWORK": "true",
            "HERALD_SANDBOX_READABLE": "/opt/agent, /srv/tools",
            "HERALD_SANDBOX_ENV": "OPENAI_API_KEY",
        }
    )

    assert policy.allow_network is True
    assert "/opt/agent" in policy.readable_paths
    assert "/srv/tools" in policy.readable_paths
    assert "OPENAI_API_KEY" in policy.env_allowlist


def test_policy_from_env_adds_the_harness_binary_directory() -> None:
    import sys

    policy = sandbox_policy_from_env({"HERALD_HARNESS_BINARY": sys.executable})

    assert str(Path(sys.executable).resolve().parent) in policy.readable_paths


def test_an_unavailable_binary_raises() -> None:
    sandbox = BubblewrapSandbox(binary="bwrap-does-not-exist")

    with pytest.raises(SandboxError):
        sandbox.wrap(["true"], worktree=Path("/tmp/wt"))


@pytest.mark.skipif(not HAS_BWRAP, reason="bubblewrap not installed")
def test_bubblewrap_actually_runs_a_command(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    sandbox = BubblewrapSandbox()

    wrapped = sandbox.wrap(["sh", "-c", "echo sandboxed > out.txt"], worktree=worktree)
    result = subprocess.run(wrapped, cwd=worktree, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert (worktree / "out.txt").read_text().strip() == "sandboxed"

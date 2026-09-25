from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dear_agent.runners.catalog import (
    EnvHarnessCatalog,
    HarnessInfo,
    build_runner_for,
    container_sandbox,
    select_harness,
)
from dear_agent.runners.claude import ClaudeCodeRunner
from dear_agent.runners.opencode import OpenCodeRunner
from dear_agent.sandbox import (
    ContainerSandbox,
    Mount,
    NoSandbox,
    Sandbox,
    SandboxError,
    parse_mounts,
)

WORKTREE = Path("/tmp/wt")


def container_argv(argv: list[str], **kwargs: object) -> list[str]:
    # Default to no SELinux handling so the argv is the same on any host; the SELinux tests
    # opt in explicitly.
    kwargs.setdefault("selinux", "none")
    sandbox = ContainerSandbox(image="img:latest", **kwargs)  # type: ignore[arg-type]
    return sandbox.build_wrapped_argv(argv, worktree=WORKTREE)


def test_container_sandbox_satisfies_the_port() -> None:
    assert isinstance(ContainerSandbox(image="img"), Sandbox)


def test_container_runs_the_image_with_the_worktree_writable() -> None:
    wrapped = container_argv(["opencode", "run", "hi"])

    assert wrapped[0] == "podman"
    assert "img:latest" in wrapped
    assert f"{WORKTREE}:/work:rw" in wrapped
    assert wrapped[wrapped.index("--workdir") + 1] == "/work"
    assert wrapped[-4:] == ["img:latest", "opencode", "run", "hi"]


def test_container_denies_new_privileges() -> None:
    assert "no-new-privileges" in container_argv(["true"])


def test_container_network_is_configurable() -> None:
    assert container_argv(["true"])[container_argv(["true"]).index("--network") + 1] == "host"
    denied = container_argv(["true"], network="none")
    assert denied[denied.index("--network") + 1] == "none"


def test_container_mounts_are_read_only_by_default() -> None:
    wrapped = container_argv(["true"], mounts=(Mount("/host/.cfg", "/root/.cfg"),))

    assert "/host/.cfg:/root/.cfg:ro" in wrapped


def test_container_mounts_can_be_writable() -> None:
    wrapped = container_argv(["true"], mounts=(Mount("/host/.cfg", "/root/.cfg", readonly=False),))

    assert "/host/.cfg:/root/.cfg:rw" in wrapped


def test_container_mount_expands_the_image_home() -> None:
    wrapped = container_argv(
        ["true"],
        mounts=(Mount("~/.claude", "~/.claude", False),),
        container_home="/home/node",
    )

    assert f"{Path.home()}/.claude:/home/node/.claude:rw" in wrapped


def test_container_passes_only_the_allowlisted_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", "{}")
    monkeypatch.setenv("SECRET_TOKEN", "nope")

    wrapped = container_argv(["true"], env_allowlist=("OPENCODE_CONFIG_CONTENT",))

    assert "OPENCODE_CONFIG_CONTENT={}" in wrapped
    assert not any("SECRET_TOKEN" in part for part in wrapped)


def test_an_unavailable_container_binary_raises() -> None:
    sandbox = ContainerSandbox(image="img", binary="podman-does-not-exist")

    with pytest.raises(SandboxError):
        sandbox.wrap(["true"], worktree=WORKTREE)


def test_parse_mounts_reads_mode_and_defaults_to_read_only() -> None:
    mounts = parse_mounts("~/.a:~/.a, /host/b:/root/b:rw")

    assert mounts[0] == Mount("~/.a", "~/.a", readonly=True)
    assert mounts[1] == Mount("/host/b", "/root/b", readonly=False)
    assert parse_mounts("") == []
    assert parse_mounts(None) == []


@pytest.mark.parametrize("value", ["/host", "/host:", ":ro", "/host:/root:rwx"])
def test_parse_mounts_rejects_malformed_entries(value: str) -> None:
    with pytest.raises(SandboxError):
        parse_mounts(value)


def test_container_relabels_the_worktree_on_selinux(monkeypatch) -> None:
    monkeypatch.setattr("dear_agent.sandbox._selinux_enabled", lambda: True)

    wrapped = container_argv(["true"], selinux="auto")

    assert f"{WORKTREE}:/work:rw,Z" in wrapped


def test_container_does_not_relabel_without_selinux(monkeypatch) -> None:
    monkeypatch.setattr("dear_agent.sandbox._selinux_enabled", lambda: False)

    wrapped = container_argv(["true"], selinux="auto")

    assert f"{WORKTREE}:/work:rw" in wrapped


def test_container_auto_leaves_mounts_alone_on_selinux(monkeypatch) -> None:
    monkeypatch.setattr("dear_agent.sandbox._selinux_enabled", lambda: True)

    wrapped = container_argv(["true"], selinux="auto", mounts=(Mount("/h/.cfg", "/root/.cfg"),))

    assert "/h/.cfg:/root/.cfg:ro" in wrapped


def test_container_relabels_every_mount_with_Z() -> None:
    wrapped = container_argv(["true"], selinux="Z", mounts=(Mount("/h/.cfg", "/root/.cfg"),))

    assert f"{WORKTREE}:/work:rw,Z" in wrapped
    assert "/h/.cfg:/root/.cfg:ro,Z" in wrapped


def test_container_can_disable_selinux_labels() -> None:
    wrapped = container_argv(["true"], selinux="disable")

    assert "label=disable" in wrapped
    assert f"{WORKTREE}:/work:rw" in wrapped


def test_container_selinux_none_never_relabels(monkeypatch) -> None:
    monkeypatch.setattr("dear_agent.sandbox._selinux_enabled", lambda: True)

    wrapped = container_argv(["true"], selinux="none", mounts=(Mount("/h/.cfg", "/root/.cfg"),))

    assert f"{WORKTREE}:/work:rw" in wrapped
    assert "/h/.cfg:/root/.cfg:ro" in wrapped


def test_container_rejects_an_unknown_selinux_mode() -> None:
    with pytest.raises(SandboxError):
        ContainerSandbox(image="img", selinux="bogus")


def test_container_sandbox_selinux_from_env() -> None:
    info = select_harness(EnvHarnessCatalog(env={}), "opencode")

    sandbox = container_sandbox(info, {"DEAR_AGENT_HARNESS_CONTAINER_SELINUX": "Z"})

    assert sandbox.selinux == "Z"


def test_container_drops_capabilities() -> None:
    wrapped = container_argv(["true"])

    assert "--cap-drop" in wrapped
    assert "ALL" in wrapped


def test_container_can_keep_capabilities() -> None:
    wrapped = container_argv(["true"], cap_drop=False)

    assert "--cap-drop" not in wrapped


def test_container_sets_a_pids_limit() -> None:
    wrapped = container_argv(["true"])
    assert wrapped[wrapped.index("--pids-limit") + 1] == "512"

    custom = container_argv(["true"], pids_limit=128)
    assert custom[custom.index("--pids-limit") + 1] == "128"


def test_container_can_omit_the_pids_limit() -> None:
    assert "--pids-limit" not in container_argv(["true"], pids_limit=None)


def test_container_can_use_a_read_only_rootfs() -> None:
    wrapped = container_argv(["true"], read_only=True)

    assert "--read-only" in wrapped
    assert "/tmp" in wrapped


def test_container_rejects_a_non_positive_pids_limit() -> None:
    with pytest.raises(SandboxError):
        ContainerSandbox(image="img", pids_limit=0)


def test_container_sandbox_hardening_from_env() -> None:
    info = select_harness(EnvHarnessCatalog(env={}), "opencode")

    sandbox = container_sandbox(
        info,
        {
            "DEAR_AGENT_HARNESS_CONTAINER_READONLY": "true",
            "DEAR_AGENT_HARNESS_CONTAINER_PIDS": "256",
            "DEAR_AGENT_HARNESS_CONTAINER_CAP_DROP": "false",
        },
    )

    assert sandbox.read_only is True
    assert sandbox.pids_limit == 256
    assert sandbox.cap_drop is False

    assert container_sandbox(info, {"DEAR_AGENT_HARNESS_CONTAINER_PIDS": "none"}).pids_limit is None


def test_container_sandbox_uses_the_harness_image_and_mounts() -> None:
    info = select_harness(EnvHarnessCatalog(env={}), "opencode")

    sandbox = container_sandbox(info, {})

    assert sandbox.image == "ghcr.io/anomalyco/opencode:latest"
    assert Mount("~/.local/share/opencode", "~/.local/share/opencode") in sandbox.mounts
    assert "OPENCODE_CONFIG_CONTENT" in sandbox.env_allowlist


def test_container_sandbox_requires_an_image_when_the_harness_has_none() -> None:
    info = HarnessInfo(id="codex", binary="codex")

    with pytest.raises(RuntimeError):
        container_sandbox(info, {})

    sandbox = container_sandbox(info, {"DEAR_AGENT_HARNESS_IMAGE": "img"})
    assert sandbox.image == "img"


def test_container_sandbox_env_overrides_the_metadata() -> None:
    info = select_harness(EnvHarnessCatalog(env={}), "opencode")

    sandbox = container_sandbox(
        info,
        {
            "DEAR_AGENT_HARNESS_IMAGE": "custom:1",
            "DEAR_AGENT_HARNESS_MOUNTS": "/h:/c:rw",
            "DEAR_AGENT_HARNESS_CONTAINER_NETWORK": "none",
            "DEAR_AGENT_HARNESS_CONTAINER_HOME": "/home/node",
            "DEAR_AGENT_HARNESS_CONTAINER_ARGS": "--cpus,2",
        },
    )

    assert sandbox.image == "custom:1"
    assert sandbox.mounts == (Mount("/h", "/c", readonly=False),)
    assert sandbox.network == "none"
    assert sandbox.container_home == "/home/node"
    assert sandbox.extra_args == ("--cpus", "2")


def test_build_runner_uses_a_container_under_podman(monkeypatch) -> None:
    from dear_agent.worker_factory import build_runner

    monkeypatch.setenv("DEAR_AGENT_ISOLATION", "podman")
    monkeypatch.setenv("DEAR_AGENT_HARNESS", "opencode")

    runner = build_runner(None, sandbox=NoSandbox())

    assert isinstance(runner, OpenCodeRunner)
    assert isinstance(runner.sandbox, ContainerSandbox)
    assert runner.sandbox.image == "ghcr.io/anomalyco/opencode:latest"


def test_build_runner_container_requires_an_image_for_a_bare_harness(monkeypatch) -> None:
    from dear_agent.worker_factory import build_runner

    monkeypatch.setenv("DEAR_AGENT_ISOLATION", "podman")
    monkeypatch.setenv("DEAR_AGENT_HARNESS", "codex")
    monkeypatch.delenv("DEAR_AGENT_HARNESS_IMAGE", raising=False)

    with pytest.raises(RuntimeError):
        build_runner(None, sandbox=NoSandbox())


def test_build_runner_bwrap_does_not_containerize(monkeypatch) -> None:
    from dear_agent.worker_factory import build_runner

    monkeypatch.setenv("DEAR_AGENT_HARNESS", "claude")
    monkeypatch.delenv("DEAR_AGENT_ISOLATION", raising=False)
    monkeypatch.delenv("DEAR_AGENT_HARNESS_IMAGE", raising=False)

    runner = build_runner(None, sandbox=NoSandbox())

    assert isinstance(runner, ClaudeCodeRunner)
    assert isinstance(runner.sandbox, NoSandbox)


def test_build_runner_for_passes_the_sandbox_to_every_harness() -> None:
    catalog = {info.id: info for info in EnvHarnessCatalog(env={}).list_harnesses()}
    sandbox = ContainerSandbox(image="img")

    claude = build_runner_for(catalog["claude"], None, sandbox)
    opencode = build_runner_for(catalog["opencode"], None, sandbox)

    assert claude.sandbox is sandbox
    assert opencode.sandbox is sandbox


def test_build_runner_for_passes_the_sandbox_to_the_command_harness() -> None:
    from dear_agent.runners.command import CommandRunner

    info = HarnessInfo(id="command", binary="my-harness --flag")
    sandbox = ContainerSandbox(image="img")

    runner = build_runner_for(info, None, sandbox)

    assert isinstance(runner, CommandRunner)
    assert runner.sandbox is sandbox


def _record_subprocess(calls: list[list[str]]):
    def run(argv, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout="", stderr="")

    return run


def test_container_session_starts_once_and_execs_later_commands(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("dear_agent.sandbox.subprocess.run", _record_subprocess(calls))
    sandbox = ContainerSandbox(image="img:latest", selinux="none")

    first = sandbox.wrap(["opencode", "run", "hi"], worktree=WORKTREE)
    second = sandbox.wrap(["make", "test"], worktree=WORKTREE)

    starts = [c for c in calls if len(c) > 1 and c[1] == "run"]
    assert len(starts) == 1
    assert "-d" in starts[0]
    assert "--rm" not in starts[0]  # the session must outlive the harness run
    name = sandbox._container
    assert name is not None
    assert first[:3] == ["podman", "exec", "--workdir"]
    assert first[-4:] == [name, "opencode", "run", "hi"]
    assert second[-2:] == ["make", "test"]
    assert sandbox._container == name  # not restarted for the verify gate


def test_container_session_close_removes_the_container_once(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("dear_agent.sandbox.subprocess.run", _record_subprocess(calls))
    sandbox = ContainerSandbox(image="img:latest", selinux="none")
    sandbox.wrap(["true"], worktree=WORKTREE)
    name = sandbox._container

    sandbox.close()
    sandbox.close()

    assert calls.count(["podman", "rm", "-f", name]) == 1
    assert sandbox._container is None


def test_container_exec_before_the_session_starts_raises() -> None:
    with pytest.raises(SandboxError):
        ContainerSandbox(image="img").build_exec_argv(["true"])

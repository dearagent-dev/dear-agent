from __future__ import annotations

from pathlib import Path

import pytest

from herald.runners.catalog import (
    EnvHarnessCatalog,
    HarnessInfo,
    build_runner_for,
    container_sandbox,
    select_harness,
)
from herald.runners.claude import ClaudeCodeRunner
from herald.runners.opencode import OpenCodeRunner
from herald.sandbox import (
    ContainerSandbox,
    Mount,
    NoSandbox,
    Sandbox,
    SandboxError,
    parse_mounts,
)

WORKTREE = Path("/tmp/wt")


def container_argv(argv: list[str], **kwargs: object) -> list[str]:
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

    sandbox = container_sandbox(info, {"HERALD_HARNESS_IMAGE": "img"})
    assert sandbox.image == "img"


def test_container_sandbox_env_overrides_the_metadata() -> None:
    info = select_harness(EnvHarnessCatalog(env={}), "opencode")

    sandbox = container_sandbox(
        info,
        {
            "HERALD_HARNESS_IMAGE": "custom:1",
            "HERALD_HARNESS_MOUNTS": "/h:/c:rw",
            "HERALD_HARNESS_CONTAINER_NETWORK": "none",
            "HERALD_HARNESS_CONTAINER_HOME": "/home/node",
            "HERALD_HARNESS_CONTAINER_ARGS": "--cpus,2",
        },
    )

    assert sandbox.image == "custom:1"
    assert sandbox.mounts == (Mount("/h", "/c", readonly=False),)
    assert sandbox.network == "none"
    assert sandbox.container_home == "/home/node"
    assert sandbox.extra_args == ("--cpus", "2")


def test_build_runner_uses_a_container_under_podman(monkeypatch) -> None:
    from herald.worker_factory import build_runner

    monkeypatch.setenv("HERALD_ISOLATION", "podman")
    monkeypatch.setenv("HERALD_HARNESS", "opencode")

    runner = build_runner(None, sandbox=NoSandbox())

    assert isinstance(runner, OpenCodeRunner)
    assert isinstance(runner.sandbox, ContainerSandbox)
    assert runner.sandbox.image == "ghcr.io/anomalyco/opencode:latest"


def test_build_runner_container_requires_an_image_for_a_bare_harness(monkeypatch) -> None:
    from herald.worker_factory import build_runner

    monkeypatch.setenv("HERALD_ISOLATION", "podman")
    monkeypatch.setenv("HERALD_HARNESS", "codex")
    monkeypatch.delenv("HERALD_HARNESS_IMAGE", raising=False)

    with pytest.raises(RuntimeError):
        build_runner(None, sandbox=NoSandbox())


def test_build_runner_bwrap_does_not_containerize(monkeypatch) -> None:
    from herald.worker_factory import build_runner

    monkeypatch.setenv("HERALD_HARNESS", "claude")
    monkeypatch.delenv("HERALD_ISOLATION", raising=False)
    monkeypatch.delenv("HERALD_HARNESS_IMAGE", raising=False)

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

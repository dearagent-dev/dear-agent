from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dear_agent.environment.bundle import (
    OPENCODE_FILES,
    BundleFile,
    HarnessBundle,
    opencode_bundle,
)
from dear_agent.sandbox import Mount


def completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def make_bundle(cache_dir: Path, run, **kwargs: object) -> HarnessBundle:
    return HarnessBundle(
        image="harness:1",
        files=(BundleFile("/usr/local/bin/opencode", "opencode"),),
        interpreter="ld-musl-x86_64.so.1",
        binary="opencode",
        cache_dir=cache_dir,
        _run=run,
        **kwargs,  # type: ignore[arg-type]
    )


def test_bundle_copies_each_file_and_writes_a_shim(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        return completed(stdout="cid123\n") if argv[1] == "create" else completed()

    bundle = make_bundle(tmp_path, run)

    bundle.ensure()

    assert ["podman", "create", "harness:1"] in calls
    assert [
        "podman",
        "cp",
        "cid123:/usr/local/bin/opencode",
        str(tmp_path / "root" / "opencode"),
    ] in calls
    assert ["podman", "rm", "-f", "cid123"] in calls
    assert bundle.ready
    shim = bundle.shim.read_text()
    assert shim.startswith("#!/bin/sh")
    assert "LD_LIBRARY_PATH=/opt/dear-agent/harness" in shim
    assert "ld-musl-x86_64.so.1 /opt/dear-agent/harness/opencode" in shim


def test_bundle_ensure_is_idempotent(tmp_path: Path) -> None:
    (tmp_path / ".ready").write_text("ok\n")
    calls: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        return completed()

    make_bundle(tmp_path, run).ensure()

    assert calls == []


def test_bundle_ensure_raises_when_create_fails(tmp_path: Path) -> None:
    def run(argv: list[str]) -> subprocess.CompletedProcess:
        return completed(returncode=1, stderr="no image")

    with pytest.raises(RuntimeError, match="no image"):
        make_bundle(tmp_path, run).ensure()


def test_bundle_ensure_removes_the_container_when_a_copy_fails(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        if argv[1] == "create":
            return completed(stdout="cid")
        if argv[1] == "cp":
            return completed(returncode=1, stderr="missing")
        return completed()

    with pytest.raises(RuntimeError, match="missing"):
        make_bundle(tmp_path, run).ensure()

    assert ["podman", "rm", "-f", "cid"] in calls


def test_bundle_mounts_expose_the_root_and_the_shim(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, lambda argv: completed())

    mounts = bundle.mounts()

    assert Mount(str(tmp_path / "root"), "/opt/dear-agent/harness", readonly=True) in mounts
    assert (
        Mount(str(tmp_path / "bin" / "opencode"), "/usr/local/bin/opencode", readonly=True)
        in mounts
    )


def test_opencode_bundle_recipe_is_musl_aware() -> None:
    bundle = opencode_bundle("ghcr.io/anomalyco/opencode:latest", cache_dir=Path("/tmp/cache"))

    assert bundle.binary == "opencode"
    assert bundle.interpreter == "ld-musl-x86_64.so.1"
    names = {item.name for item in bundle.files}
    assert {"opencode", "ld-musl-x86_64.so.1", "libstdc++.so.6", "libgcc_s.so.1"} <= names
    assert bundle.files == OPENCODE_FILES


def test_bundle_injection_is_off_by_default(monkeypatch) -> None:
    from dear_agent.runners.catalog import HarnessInfo
    from dear_agent.sandbox import ContainerSandbox
    from dear_agent.worker_factory import _maybe_inject_bundle

    monkeypatch.delenv("DEAR_AGENT_HARNESS_BUNDLE", raising=False)
    session = ContainerSandbox(image="env:1")

    result = _maybe_inject_bundle(session, HarnessInfo(id="opencode", binary="opencode"))

    assert result is session


def test_bundle_injection_requires_container_isolation(monkeypatch) -> None:
    from dear_agent.runners.catalog import HarnessInfo
    from dear_agent.sandbox import NoSandbox
    from dear_agent.worker_factory import _maybe_inject_bundle

    monkeypatch.setenv("DEAR_AGENT_HARNESS_BUNDLE", "true")

    with pytest.raises(RuntimeError):
        _maybe_inject_bundle(NoSandbox(), HarnessInfo(id="opencode", binary="opencode"))


def test_bundle_injection_adds_the_bundle_mounts(monkeypatch) -> None:
    import dear_agent.environment.bundle as bundle_module
    from dear_agent.runners.catalog import HarnessInfo
    from dear_agent.sandbox import ContainerSandbox
    from dear_agent.worker_factory import _maybe_inject_bundle

    class FakeBundle:
        def __init__(self) -> None:
            self.ensured = False

        def ensure(self) -> None:
            self.ensured = True

        def mounts(self) -> tuple[Mount, ...]:
            return (Mount("/bundle/root", "/opt/dear-agent/harness"),)

    fake = FakeBundle()
    monkeypatch.setenv("DEAR_AGENT_HARNESS_BUNDLE", "true")
    monkeypatch.setattr(bundle_module, "opencode_bundle", lambda *a, **k: fake)
    session = ContainerSandbox(image="env:1")

    result = _maybe_inject_bundle(
        session, HarnessInfo(id="opencode", binary="opencode", image="oc:1")
    )

    assert result.mounts == (Mount("/bundle/root", "/opt/dear-agent/harness"),)
    assert fake.ensured is True


def test_runner_uses_the_declared_environment_and_injects_the_harness(
    monkeypatch, tmp_path
) -> None:
    import dear_agent.environment.bundle as bundle_module
    import dear_agent.worker_factory as wf
    from dear_agent.environment.descriptor import EnvironmentDescriptor
    from dear_agent.runners.opencode import OpenCodeRunner
    from dear_agent.sandbox import ContainerSandbox, NoSandbox

    class FakeBundle:
        def ensure(self) -> None:
            pass

        def mounts(self) -> tuple[Mount, ...]:
            return ()

    monkeypatch.setenv("DEAR_AGENT_ISOLATION", "podman")
    monkeypatch.setenv("DEAR_AGENT_HARNESS", "opencode")
    monkeypatch.delenv("DEAR_AGENT_HARNESS_IMAGE", raising=False)
    monkeypatch.delenv("DEAR_AGENT_HARNESS_BUNDLE", raising=False)
    monkeypatch.setattr(
        wf, "detect", lambda root: EnvironmentDescriptor(kind="devcontainer", image="env:1")
    )
    monkeypatch.setattr(bundle_module, "opencode_bundle", lambda *a, **k: FakeBundle())

    runner, session = wf.build_runner_and_session(None, NoSandbox(), repo_path=str(tmp_path))

    assert isinstance(runner, OpenCodeRunner)
    assert isinstance(session, ContainerSandbox)
    assert session.image == "env:1"  # the repository declared the environment


def test_runner_without_a_descriptor_uses_the_harness_image(monkeypatch, tmp_path) -> None:
    from dear_agent.sandbox import ContainerSandbox, NoSandbox
    from dear_agent.worker_factory import build_runner_and_session

    monkeypatch.setenv("DEAR_AGENT_ISOLATION", "podman")
    monkeypatch.setenv("DEAR_AGENT_HARNESS", "opencode")
    monkeypatch.delenv("DEAR_AGENT_HARNESS_IMAGE", raising=False)
    monkeypatch.delenv("DEAR_AGENT_HARNESS_BUNDLE", raising=False)

    _, session = build_runner_and_session(None, NoSandbox(), repo_path=str(tmp_path))

    assert isinstance(session, ContainerSandbox)
    assert session.image == "ghcr.io/anomalyco/opencode:latest"


def test_build_environment_builds_and_uses_the_image(monkeypatch, tmp_path) -> None:
    import dear_agent.environment.bundle as bundle_module
    import dear_agent.worker_factory as wf
    from dear_agent.environment.descriptor import EnvironmentDescriptor
    from dear_agent.sandbox import NoSandbox

    class FakeBuilder:
        def __init__(self, **kwargs: object) -> None:
            pass

        def build(self, descriptor, *, workspace, image_name=None, harness_feature=None):
            return "env:built"

    class FakeBundle:
        def ensure(self) -> None:
            pass

        def mounts(self) -> tuple[Mount, ...]:
            return ()

    monkeypatch.setenv("DEAR_AGENT_ISOLATION", "podman")
    monkeypatch.setenv("DEAR_AGENT_HARNESS", "opencode")
    monkeypatch.setenv("DEAR_AGENT_BUILD_ENVIRONMENT", "true")
    monkeypatch.delenv("DEAR_AGENT_HARNESS_IMAGE", raising=False)
    monkeypatch.delenv("DEAR_AGENT_HARNESS_BUNDLE", raising=False)
    monkeypatch.setattr(
        wf,
        "detect",
        lambda root: EnvironmentDescriptor(kind="devcontainer", path=tmp_path / "dc.json"),
    )
    monkeypatch.setattr(wf, "EnvironmentBuilder", FakeBuilder)
    monkeypatch.setattr(bundle_module, "opencode_bundle", lambda *a, **k: FakeBundle())

    _, session = wf.build_runner_and_session(None, NoSandbox(), repo_path=str(tmp_path))

    assert session.image == "env:built"


def test_harness_feature_bakes_the_harness_and_skips_the_bundle(monkeypatch, tmp_path) -> None:
    import dear_agent.environment.bundle as bundle_module
    import dear_agent.worker_factory as wf
    from dear_agent.environment.descriptor import EnvironmentDescriptor
    from dear_agent.sandbox import NoSandbox

    class FakeBuilder:
        def __init__(self, **kwargs: object) -> None:
            pass

        def build(self, descriptor, *, workspace, image_name=None, harness_feature=None):
            assert harness_feature == "ghcr.io/example/opencode:1"
            return "env:featured"

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("bundle must not be injected when a harness feature is baked in")

    monkeypatch.setenv("DEAR_AGENT_ISOLATION", "podman")
    monkeypatch.setenv("DEAR_AGENT_HARNESS", "opencode")
    monkeypatch.setenv("DEAR_AGENT_BUILD_ENVIRONMENT", "true")
    monkeypatch.setenv("DEAR_AGENT_HARNESS_FEATURE", "ghcr.io/example/opencode:1")
    monkeypatch.delenv("DEAR_AGENT_HARNESS_IMAGE", raising=False)
    monkeypatch.delenv("DEAR_AGENT_HARNESS_BUNDLE", raising=False)
    monkeypatch.setattr(
        wf,
        "detect",
        lambda root: EnvironmentDescriptor(kind="devcontainer", path=tmp_path / "dc.json"),
    )
    monkeypatch.setattr(wf, "EnvironmentBuilder", FakeBuilder)
    monkeypatch.setattr(bundle_module, "opencode_bundle", explode)

    _, session = wf.build_runner_and_session(None, NoSandbox(), repo_path=str(tmp_path))

    assert session.image == "env:featured"

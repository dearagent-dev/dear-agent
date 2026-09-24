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

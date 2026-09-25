from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dear_agent.environment.builder import EnvironmentBuilder, EnvironmentBuildError
from dear_agent.environment.descriptor import EnvironmentDescriptor


def completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_containerfile_descriptor_is_built_with_podman(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    dockerfile = tmp_path / "Containerfile"
    dockerfile.write_text("FROM scratch\n")
    descriptor = EnvironmentDescriptor(
        kind="containerfile", path=dockerfile, dockerfile=dockerfile, context=tmp_path
    )

    image = EnvironmentBuilder(_run=lambda argv: calls.append(list(argv)) or completed()).build(
        descriptor, workspace=tmp_path, image_name="env:1"
    )

    assert image == "env:1"
    assert calls[0] == [
        "podman",
        "build",
        "-t",
        "env:1",
        "-f",
        str(dockerfile),
        str(tmp_path),
    ]


def test_devcontainer_is_built_with_the_cli(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    descriptor = EnvironmentDescriptor(kind="devcontainer", path=tmp_path / "devcontainer.json")

    image = EnvironmentBuilder(_run=lambda argv: calls.append(list(argv)) or completed()).build(
        descriptor, workspace=tmp_path, image_name="env:1"
    )

    assert image == "env:1"
    assert calls[0][:4] == ["devcontainer", "build", "--workspace-folder", str(tmp_path)]
    assert "--image-name" in calls[0] and "env:1" in calls[0]
    assert "--additional-features" not in calls[0]


def test_harness_feature_is_added_to_the_devcontainer_build(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    descriptor = EnvironmentDescriptor(kind="devcontainer", path=tmp_path / "devcontainer.json")

    EnvironmentBuilder(_run=lambda argv: calls.append(list(argv)) or completed()).build(
        descriptor,
        workspace=tmp_path,
        harness_feature="ghcr.io/example/opencode:1",
    )

    argv = calls[0]
    features = argv[argv.index("--additional-features") + 1]
    assert json.loads(features) == {"ghcr.io/example/opencode:1": {}}


def test_a_failed_build_raises_with_the_output(tmp_path: Path) -> None:
    descriptor = EnvironmentDescriptor(
        kind="containerfile", path=tmp_path / "Containerfile", dockerfile=tmp_path / "Containerfile"
    )

    with pytest.raises(EnvironmentBuildError, match="boom"):
        EnvironmentBuilder(_run=lambda argv: completed(returncode=1, stderr="boom")).build(
            descriptor, workspace=tmp_path
        )


def test_a_descriptor_without_an_image_or_build_raises(tmp_path: Path) -> None:
    descriptor = EnvironmentDescriptor(kind="mise", path=tmp_path / "mise.toml")

    with pytest.raises(EnvironmentBuildError, match="no image or build"):
        EnvironmentBuilder(_run=lambda argv: completed()).build(descriptor, workspace=tmp_path)

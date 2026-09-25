from __future__ import annotations

from pathlib import Path

from dear_agent.environment.descriptor import EnvironmentDescriptor, detect


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_no_descriptor_falls_back_to_none(tmp_path: Path) -> None:
    descriptor = detect(tmp_path)

    assert descriptor == EnvironmentDescriptor(kind="none")
    assert not descriptor.has_image


def test_devcontainer_image_is_preferred_and_comments_are_tolerated(tmp_path: Path) -> None:
    write(
        tmp_path / ".devcontainer/devcontainer.json",
        """{
  // a comment
  "image": "mcr.microsoft.com/devcontainers/python:3.12",
  "features": {"ghcr.io/devcontainers/features/node:1": {}}
}""",
    )

    descriptor = detect(tmp_path)

    assert descriptor.kind == "devcontainer"
    assert descriptor.image == "mcr.microsoft.com/devcontainers/python:3.12"
    assert "1 dev container feature" in descriptor.detail


def test_devcontainer_url_with_slashes_is_not_stripped_as_a_comment(tmp_path: Path) -> None:
    write(
        tmp_path / ".devcontainer.json",
        '{\n  "image": "ghcr.io/example/env:1",\n  "customizations": {"x": "http://example.com"}\n}',
    )

    descriptor = detect(tmp_path)

    assert descriptor.image == "ghcr.io/example/env:1"


def test_devcontainer_build_recipe_is_returned_when_there_is_no_image(tmp_path: Path) -> None:
    write(
        tmp_path / ".devcontainer/devcontainer.json",
        '{"build": {"dockerfile": "Dockerfile", "context": ".."}}',
    )
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")

    descriptor = detect(tmp_path)

    assert descriptor.has_build
    assert descriptor.image is None
    assert descriptor.dockerfile == (tmp_path / ".devcontainer/Dockerfile").resolve()


def test_devcontainer_in_a_subfolder_is_found(tmp_path: Path) -> None:
    write(
        tmp_path / ".devcontainer/backend/devcontainer.json",
        '{"image": "ghcr.io/example/backend:1"}',
    )

    assert detect(tmp_path).image == "ghcr.io/example/backend:1"


def test_an_unreadable_devcontainer_is_reported_not_skipped(tmp_path: Path) -> None:
    write(tmp_path / ".devcontainer/devcontainer.json", "{ this is not json")

    descriptor = detect(tmp_path)

    assert descriptor.kind == "devcontainer"
    assert descriptor.image is None
    assert "unreadable" in descriptor.detail


def test_ansible_execution_environment_base_image(tmp_path: Path) -> None:
    write(
        tmp_path / "execution-environment.yml",
        "images:\n  base_image:\n    name: registry.redhat.io/ee-minimal-rhel9:2.16\n",
    )

    descriptor = detect(tmp_path)

    assert descriptor.kind == "ansible-ee"
    assert descriptor.image == "registry.redhat.io/ee-minimal-rhel9:2.16"


def test_containerfile_is_a_build_recipe(tmp_path: Path) -> None:
    write(tmp_path / "Containerfile", "FROM registry.access.redhat.com/ubi9/ubi\n")

    descriptor = detect(tmp_path)

    assert descriptor.kind == "containerfile"
    assert descriptor.dockerfile == tmp_path / "Containerfile"
    assert descriptor.context == tmp_path


def test_tool_versions_are_recognized_without_an_image(tmp_path: Path) -> None:
    write(tmp_path / ".tool-versions", "python 3.12.5\n")

    descriptor = detect(tmp_path)

    assert descriptor.kind == "mise"
    assert descriptor.image is None


def test_devcontainer_wins_over_a_containerfile(tmp_path: Path) -> None:
    write(tmp_path / "Containerfile", "FROM scratch\n")
    write(tmp_path / ".devcontainer/devcontainer.json", '{"image": "env:1"}')

    assert detect(tmp_path).kind == "devcontainer"


def test_devcontainer_build_escaping_the_repository_is_rejected(tmp_path: Path) -> None:
    write(
        tmp_path / ".devcontainer/devcontainer.json",
        '{"build": {"dockerfile": "../../evil/Dockerfile"}}',
    )

    descriptor = detect(tmp_path)

    assert descriptor.kind == "devcontainer"
    assert not descriptor.has_build
    assert "outside the repository" in descriptor.detail


def test_devcontainer_absolute_build_path_is_rejected(tmp_path: Path) -> None:
    write(
        tmp_path / ".devcontainer/devcontainer.json",
        '{"build": {"dockerfile": "/etc/passwd"}}',
    )

    assert not detect(tmp_path).has_build


def test_devcontainer_context_escaping_the_repository_is_rejected(tmp_path: Path) -> None:
    write(tmp_path / "Dockerfile", "FROM scratch\n")
    write(
        tmp_path / ".devcontainer/devcontainer.json",
        '{"build": {"dockerfile": "../Dockerfile", "context": "../../.."}}',
    )

    assert not detect(tmp_path).has_build

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dear_agent.environment.descriptor import EnvironmentDescriptor

DEFAULT_CONTAINER_BINARY = "podman"
DEFAULT_DEVCONTAINER_BINARY = "devcontainer"
DEFAULT_IMAGE_NAME = "dear-agent-env:latest"

Run = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class EnvironmentBuildError(RuntimeError):
    """A repository's environment image could not be built."""


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


@dataclass(slots=True)
class EnvironmentBuilder:
    """Builds the environment image a repository declares (ADR 0008).

    A ``Containerfile``/Dockerfile descriptor is built with ``podman build``; a Dev Container is
    built with the ``devcontainer`` CLI, which also installs its ``features``. A harness Feature
    (``harness_feature``) can be added on top, so the harness is baked into the image instead of
    injected as a bundle. The prebuilt image is what the runner then starts; nothing is built at
    run time in the cluster (a CI / BuildConfig prebuild uses the same code path).
    """

    container_binary: str = DEFAULT_CONTAINER_BINARY
    devcontainer_binary: str = DEFAULT_DEVCONTAINER_BINARY
    _run: Run = field(default=_run, repr=False)

    def build(
        self,
        descriptor: EnvironmentDescriptor,
        *,
        workspace: str | Path,
        image_name: str = DEFAULT_IMAGE_NAME,
        harness_feature: str | None = None,
    ) -> str:
        """Build the image and return its name."""
        if descriptor.kind == "devcontainer":
            return self._build_devcontainer(
                workspace, image_name=image_name, harness_feature=harness_feature
            )
        if descriptor.has_build:
            return self._build_dockerfile(descriptor, image_name=image_name)
        raise EnvironmentBuildError(
            f"{descriptor.kind} descriptor at {descriptor.path} has no image or build"
        )

    def _build_dockerfile(self, descriptor: EnvironmentDescriptor, *, image_name: str) -> str:
        dockerfile = descriptor.dockerfile
        assert dockerfile is not None  # guarded by has_build
        context = descriptor.context or dockerfile.parent
        self._checked(
            [
                self.container_binary,
                "build",
                "-t",
                image_name,
                "-f",
                str(dockerfile),
                str(context),
            ],
            "container build",
        )
        return image_name

    def _build_devcontainer(
        self, workspace: str | Path, *, image_name: str, harness_feature: str | None
    ) -> str:
        argv = [
            self.devcontainer_binary,
            "build",
            "--workspace-folder",
            str(workspace),
            "--image-name",
            image_name,
        ]
        if harness_feature:
            argv += ["--additional-features", json.dumps({harness_feature: {}})]
        self._checked(argv, "dev container build")
        return image_name

    def _checked(self, argv: list[str], what: str) -> None:
        result = self._run(argv)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise EnvironmentBuildError(f"{what} failed: {detail[:500]}")


__all__ = [
    "DEFAULT_CONTAINER_BINARY",
    "DEFAULT_DEVCONTAINER_BINARY",
    "DEFAULT_IMAGE_NAME",
    "EnvironmentBuilder",
    "EnvironmentBuildError",
]

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# The repository declares how to build its environment; Dear Agent consumes the declaration
# instead of guessing (ADR 0008). Precedence, most specific first.
DEV_CONTAINER_LOCATIONS: tuple[str, ...] = (".devcontainer/devcontainer.json", ".devcontainer.json")
ANSIBLE_EE_FILES: tuple[str, ...] = (
    "execution-environment.yml",
    "execution-environment.yaml",
    "execution_environment.yml",
    "execution_environment.yaml",
)
CONTAINERFILE_FILES: tuple[str, ...] = ("Containerfile", "Dockerfile")
TOOL_VERSION_FILES: tuple[str, ...] = ("mise.toml", ".mise.toml", ".tool-versions")


@dataclass(frozen=True, slots=True)
class EnvironmentDescriptor:
    """How a repository says its run environment should be built.

    ``image`` is a runnable base image, when the descriptor names one; ``dockerfile``/``context``
    is a build recipe, when it builds one. ``kind`` is ``devcontainer``, ``ansible-ee``,
    ``containerfile``, ``mise`` or ``none`` (the fallback: run the harness image and let the
    harness provision). ``detail`` is a short human-readable note.
    """

    kind: str
    path: Path | None = None
    image: str | None = None
    dockerfile: Path | None = None
    context: Path | None = None
    detail: str = ""

    @property
    def has_image(self) -> bool:
        return bool(self.image)

    @property
    def has_build(self) -> bool:
        return self.dockerfile is not None


def detect(root: str | Path) -> EnvironmentDescriptor:
    """Resolve the environment descriptor for a repository checkout (ADR 0008).

    The first descriptor found wins, in the order Dev Container, Ansible Execution Environment,
    ``Containerfile``/``Dockerfile``, tool-version files. No descriptor is not an error: the
    fallback runs the harness image and lets the harness provision the toolchain.
    """
    base = Path(root)
    for finder in (_find_devcontainer, _find_ansible_ee, _find_containerfile, _find_tool_versions):
        descriptor = finder(base)
        if descriptor is not None:
            return descriptor
    return EnvironmentDescriptor(kind="none")


def _find_devcontainer(root: Path) -> EnvironmentDescriptor | None:
    for rel in DEV_CONTAINER_LOCATIONS:
        candidate = root / rel
        if candidate.is_file():
            return _parse_devcontainer(candidate)
    container_dir = root / ".devcontainer"
    if container_dir.is_dir():
        for candidate in sorted(container_dir.glob("*/devcontainer.json")):
            return _parse_devcontainer(candidate)
    return None


def _parse_devcontainer(path: Path) -> EnvironmentDescriptor:
    try:
        data = _load_jsonc(path)
    except (OSError, ValueError) as exc:
        return EnvironmentDescriptor(kind="devcontainer", path=path, detail=f"unreadable: {exc}")
    if not isinstance(data, dict):
        return EnvironmentDescriptor(kind="devcontainer", path=path, detail="not a JSON object")
    image = data.get("image")
    if isinstance(image, str) and image.strip():
        return EnvironmentDescriptor(
            kind="devcontainer", path=path, image=image.strip(), detail=_features_note(data)
        )
    recipe = _devcontainer_build(data, path.parent)
    if recipe is not None:
        dockerfile, context = recipe
        detail = f"build {dockerfile.name}"
        features = _features_note(data)
        if features:
            detail = f"{detail} ({features})"
        return EnvironmentDescriptor(
            kind="devcontainer",
            path=path,
            dockerfile=dockerfile,
            context=context,
            detail=detail,
        )
    return EnvironmentDescriptor(kind="devcontainer", path=path, detail="no image or build")


def _devcontainer_build(data: dict[str, Any], directory: Path) -> tuple[Path, Path] | None:
    build = data.get("build")
    if isinstance(build, str) and build.strip():
        return (directory / build.strip()).resolve(), directory.resolve()
    if isinstance(build, dict):
        dockerfile = build.get("dockerfile") or build.get("dockerFile")
        if isinstance(dockerfile, str) and dockerfile.strip():
            context = build.get("context") or "."
            return (directory / dockerfile.strip()).resolve(), (directory / str(context)).resolve()
    legacy = data.get("dockerFile")
    if isinstance(legacy, str) and legacy.strip():
        context = data.get("context") or "."
        return (directory / legacy.strip()).resolve(), (directory / str(context)).resolve()
    return None


def _features_note(data: dict[str, Any]) -> str:
    features = data.get("features")
    if isinstance(features, dict) and features:
        return f"{len(features)} dev container feature(s)"
    return ""


def _find_ansible_ee(root: Path) -> EnvironmentDescriptor | None:
    for name in ANSIBLE_EE_FILES:
        candidate = root / name
        if candidate.is_file():
            return _parse_ansible_ee(candidate)
    return None


def _parse_ansible_ee(path: Path) -> EnvironmentDescriptor:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return EnvironmentDescriptor(kind="ansible-ee", path=path, detail=f"unreadable: {exc}")
    if not isinstance(data, dict):
        return EnvironmentDescriptor(kind="ansible-ee", path=path, detail="not a mapping")
    images = data.get("images") or {}
    base = images.get("base_image") if isinstance(images, dict) else None
    name = base.get("name") if isinstance(base, dict) else None
    if isinstance(name, str) and name.strip():
        return EnvironmentDescriptor(kind="ansible-ee", path=path, image=name.strip())
    return EnvironmentDescriptor(kind="ansible-ee", path=path, detail="no images.base_image.name")


def _find_containerfile(root: Path) -> EnvironmentDescriptor | None:
    for name in CONTAINERFILE_FILES:
        candidate = root / name
        if candidate.is_file():
            return EnvironmentDescriptor(
                kind="containerfile", path=candidate, dockerfile=candidate, context=root
            )
    return None


def _find_tool_versions(root: Path) -> EnvironmentDescriptor | None:
    for name in TOOL_VERSION_FILES:
        candidate = root / name
        if candidate.is_file():
            return EnvironmentDescriptor(
                kind="mise",
                path=candidate,
                detail="tool versions; a base image must be provisioned",
            )
    return None


def _load_jsonc(path: Path) -> Any:
    """Load a JSON-with-comments file (devcontainer.json allows ``//`` and ``/* */``)."""
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_strip_jsonc(text))


def _strip_jsonc(text: str) -> str:
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escape = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            index += 2
            while index < length and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


__all__ = [
    "ANSIBLE_EE_FILES",
    "CONTAINERFILE_FILES",
    "DEV_CONTAINER_LOCATIONS",
    "TOOL_VERSION_FILES",
    "EnvironmentDescriptor",
    "detect",
]

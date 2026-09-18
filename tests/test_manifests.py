from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
HAS_KUSTOMIZE = shutil.which("kustomize") is not None

pytestmark = pytest.mark.skipif(not HAS_KUSTOMIZE, reason="kustomize not installed")


def build(overlay: str) -> list[dict]:
    result = subprocess.run(
        ["kustomize", "build", str(REPO_ROOT / "deploy" / "overlays" / overlay)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


@pytest.mark.parametrize("overlay", ["dev", "prod"])
def test_overlay_builds(overlay: str) -> None:
    docs = build(overlay)

    assert any(doc.get("kind") == "Deployment" for doc in docs)
    assert any(doc.get("kind") == "CronJob" for doc in docs)
    assert any(doc.get("kind") == "Job" for doc in docs)


def test_every_pod_disables_service_account_token_mounting() -> None:
    docs = build("dev")
    pods = [doc for doc in docs if doc.get("kind") in {"Deployment", "Job", "CronJob"}]

    for doc in pods:
        spec = doc["spec"]
        pod_spec = (
            spec["template"]["spec"]
            if doc["kind"] != "CronJob"
            else spec["jobTemplate"]["spec"]["template"]["spec"]
        )
        assert pod_spec["automountServiceAccountToken"] is False, doc["metadata"]["name"]


def test_runner_mounts_no_write_key() -> None:
    docs = build("dev")
    job = next(doc for doc in docs if doc.get("kind") == "Job")
    volumes = job["spec"]["template"]["spec"]["volumes"]
    names = {volume["name"] for volume in volumes}

    assert "git-read" in names
    assert "git-push" not in names


def test_secrets_contain_no_values_in_git() -> None:
    for doc in build("dev"):
        if doc.get("kind") != "Secret":
            continue
        assert not doc.get("data"), doc["metadata"]["name"]
        assert not doc.get("stringData"), doc["metadata"]["name"]


def test_containers_drop_privileges() -> None:
    docs = build("dev")
    for doc in docs:
        if doc.get("kind") not in {"Deployment", "Job", "CronJob"}:
            continue
        spec = doc["spec"]
        pod_spec = (
            spec["template"]["spec"]
            if doc["kind"] != "CronJob"
            else spec["jobTemplate"]["spec"]["template"]["spec"]
        )
        for container in pod_spec["containers"]:
            security = container.get("securityContext", {})
            assert security.get("allowPrivilegeEscalation") is False
            assert security.get("capabilities", {}).get("drop") == ["ALL"]

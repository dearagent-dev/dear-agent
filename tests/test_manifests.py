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


def pod_spec(doc: dict) -> dict:
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        return spec["jobTemplate"]["spec"]["template"]["spec"]
    return spec["template"]["spec"]


def pod_docs(docs: list[dict]) -> list[dict]:
    return [doc for doc in docs if doc.get("kind") in {"Deployment", "CronJob"}]


def runner_template(docs: list[dict]) -> dict:
    template = next(
        doc for doc in docs if doc.get("metadata", {}).get("name") == "herald-runner-template"
    )
    return yaml.safe_load(template["data"]["job.yaml"])


@pytest.mark.parametrize("overlay", ["dev", "prod"])
def test_overlay_builds(overlay: str) -> None:
    docs = build(overlay)

    assert any(doc.get("kind") == "Deployment" for doc in docs)
    assert any(doc.get("kind") == "CronJob" for doc in docs)
    assert runner_template(docs)["kind"] == "Job"


def test_every_pod_disables_service_account_token_mounting() -> None:
    for doc in pod_docs(build("dev")):
        assert pod_spec(doc)["automountServiceAccountToken"] is False, doc["metadata"]["name"]


def test_runner_template_mounts_no_write_key() -> None:
    job = runner_template(build("dev"))
    volumes = job["spec"]["template"]["spec"]["volumes"]
    names = {volume["name"] for volume in volumes}

    assert "git-read" in names
    assert "git-push" not in names


def test_runner_template_has_the_hardening() -> None:
    job = runner_template(build("dev"))
    pod = job["spec"]["template"]["spec"]

    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    for container in pod["containers"]:
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_secrets_contain_no_values_in_git() -> None:
    for doc in build("dev"):
        if doc.get("kind") != "Secret":
            continue
        assert not doc.get("data"), doc["metadata"]["name"]
        assert not doc.get("stringData"), doc["metadata"]["name"]


def test_containers_drop_privileges() -> None:
    for doc in pod_docs(build("dev")):
        for container in pod_spec(doc)["containers"]:
            security = container.get("securityContext", {})
            assert security.get("allowPrivilegeEscalation") is False
            assert security.get("capabilities", {}).get("drop") == ["ALL"]


def test_secret_references_are_optional_so_pods_start_without_values() -> None:
    for doc in pod_docs(build("dev")):
        for container in pod_spec(doc)["containers"]:
            for env in container.get("env", []):
                ref = env.get("valueFrom", {}).get("secretKeyRef")
                if ref:
                    assert ref.get("optional") is True, env["name"]

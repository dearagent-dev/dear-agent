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
        if doc["kind"] == "CronJob":
            continue
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


def test_no_secret_objects_are_applied_from_git() -> None:
    # Secrets are provisioned out of band; if they were in the build, a kustomize apply
    # could reset a live credential (this happened once).
    for overlay in ("dev", "prod"):
        assert all(doc.get("kind") != "Secret" for doc in build(overlay)), overlay


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


def test_dispatcher_can_create_jobs_but_not_read_secrets() -> None:
    docs = build("dev")
    role = next(doc for doc in docs if doc.get("kind") == "Role")
    rules = role["rules"]

    assert any("jobs" in rule["resources"] and "create" in rule["verbs"] for rule in rules)
    assert all("secrets" not in rule["resources"] for rule in rules)


def test_sweep_invokes_the_dispatcher() -> None:
    for overlay in ("dev", "prod"):
        cron = next(doc for doc in build(overlay) if doc.get("kind") == "CronJob")
        container = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == ["herald"]
        assert container["args"][:1] == ["sweep"]


def test_config_selects_the_postgres_queue_and_jmap_transport() -> None:
    docs = build("dev")
    config = next(
        doc
        for doc in docs
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "herald-config"
    )

    assert config["data"]["HERALD_QUEUE"] == "postgres"
    assert config["data"]["HERALD_BACKEND"] == "jmap"


def postgres_statefulset(docs: list[dict]) -> dict:
    return next(
        doc
        for doc in docs
        if doc.get("kind") == "StatefulSet" and doc["metadata"]["name"] == "herald-postgres"
    )


@pytest.mark.parametrize("overlay", ["dev", "prod"])
def test_postgres_runs_postgresql_18_on_ubi(overlay: str) -> None:
    container = postgres_statefulset(build(overlay))["spec"]["template"]["spec"]["containers"][0]

    assert container["image"].startswith("registry.redhat.io/rhel")
    assert "postgresql-18" in container["image"]


@pytest.mark.parametrize("overlay", ["dev", "prod"])
def test_postgres_is_never_exposed_outside_the_cluster(overlay: str) -> None:
    docs = build(overlay)
    service = next(
        doc
        for doc in docs
        if doc.get("kind") == "Service" and doc["metadata"]["name"] == "herald-postgres"
    )

    assert service["spec"]["clusterIP"] == "None"  # headless: no ClusterIP
    assert service["spec"].get("type", "ClusterIP") == "ClusterIP"
    assert not any(
        doc.get("kind") in {"Route", "Ingress"} and "postgres" in doc["metadata"]["name"]
        for doc in docs
    )


@pytest.mark.parametrize("overlay", ["dev", "prod"])
def test_postgres_persists_its_data(overlay: str) -> None:
    spec = postgres_statefulset(build(overlay))["spec"]
    container = spec["template"]["spec"]["containers"][0]

    assert spec["volumeClaimTemplates"]
    assert any(mount["mountPath"] == "/var/lib/pgsql/data" for mount in container["volumeMounts"])
    assert spec["template"]["spec"]["automountServiceAccountToken"] is False

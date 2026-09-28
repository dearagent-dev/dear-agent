from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import yaml

from dear_agent.deps import dependency_status
from dear_agent.queue.models import Task, TaskState
from dear_agent.queue.port import Queue

WORKER_ANNOTATION = "dearagent.dev/task-id"

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Image placeholders in the runner Job template. The dispatcher resolves them from the
# environment, so the ConfigMap does not pin an image tag (kustomize `images:` cannot rewrite a
# value inside a ConfigMap). Each maps to (env var, fallback).
IMAGE_PLACEHOLDERS: dict[str, tuple[str, str]] = {
    "__RUNNER_IMAGE__": ("DEAR_AGENT_RUNNER_IMAGE", "quay.io/dear-agent/dear-agent:latest"),
    "__HARNESS_IMAGE__": (
        "DEAR_AGENT_HARNESS_IMAGE",
        "quay.io/dear-agent/dear-agent:harness-latest",
    ),
}


class DispatchError(RuntimeError):
    """A task could not be dispatched to a runner Job."""


def job_name(task_id: str) -> str:
    """A deterministic, DNS-1123 Job name for a task id.

    Deterministic so that re-dispatching the same task is a no-op (the Job already exists),
    which is what makes the sweep idempotent across restarts and repeated CronJob runs. The
    short digest keeps distinct task ids from colliding after slugging.
    """
    prefix = "dear-agent-task-"
    digest = hashlib.sha256(task_id.encode()).hexdigest()[:10]
    max_slug = 63 - len(prefix) - 1 - len(digest)
    slug = _SLUG_RE.sub("-", task_id.lower()).strip("-")[:max_slug] or "task"
    return f"{prefix}{slug}-{digest}"


def render_job(template: str, task: Task, *, env: Mapping[str, str] | None = None) -> dict:
    """Render the runner JobTemplate for one task, injecting its id.

    The template is a Job manifest as a YAML string (as stored in the ConfigMap). We parse
    it, stamp the task id as an env var and an annotation, resolve the image placeholders from
    ``env`` (``DEAR_AGENT_RUNNER_IMAGE`` / ``DEAR_AGENT_HARNESS_IMAGE``), and give the Job a
    deterministic name. No shell interpolation is involved, so a hostile task id cannot inject
    YAML.
    """

    source = env if env is not None else os.environ
    job = yaml.safe_load(template)
    metadata = job.setdefault("metadata", {})
    metadata.pop("generateName", None)
    metadata["name"] = job_name(task.id)
    spec = job["spec"]["template"]["spec"]
    containers = spec.get("containers", [])
    if not containers:
        raise DispatchError("runner template has no containers")
    for container in containers:
        _resolve_image(container, source)
        env_list = container.setdefault("env", [])
        for entry in env_list:
            if entry.get("name") == "DEAR_AGENT_TASK_ID":
                entry["value"] = task.id
                break
        else:
            env_list.append({"name": "DEAR_AGENT_TASK_ID", "value": task.id})
    template_meta = job["spec"]["template"].setdefault("metadata", {})
    template_meta.setdefault("annotations", {})[WORKER_ANNOTATION] = task.id
    return job


def _resolve_image(container: dict, env: Mapping[str, str]) -> None:
    image = container.get("image")
    placeholder = IMAGE_PLACEHOLDERS.get(image) if isinstance(image, str) else None
    if placeholder is not None:
        variable, default = placeholder
        container["image"] = env.get(variable) or default


@dataclass(slots=True)
class TaskDispatcher:
    """Reconciles queued tasks into runner Jobs.

    For each `queued` task it renders the JobTemplate and asks the launcher to create the
    Job. Creation is idempotent on the launcher side (a Job named for the task already
    existing is success), so re-dispatching a task after a restart is safe.
    """

    queue: Queue
    launcher: Callable[[dict], None]
    template: str
    limit: int = 20

    def dispatch_once(self) -> list[str]:
        created: list[str] = []
        for task in self.queue.list(TaskState.QUEUED, limit=self.limit):
            if self._already_running(task):
                continue
            # Do not start a task whose dependencies are not met; fail the ones that can
            # never run because a dependency failed.
            status = dependency_status(self.queue, task)
            if status.state == "failed":
                self.queue.transition(task, TaskState.FAILED)
                continue
            if status.state == "blocked":
                continue
            job = render_job(self.template, task)
            self.launcher(job)
            created.append(task.id)
        return created

    def _already_running(self, task: Task) -> bool:
        return task.state is not TaskState.QUEUED


def kubernetes_launcher(client: object) -> Callable[[dict], None]:
    """Build a launcher backed by a :class:`dear_agent.kube.KubernetesClient`."""

    def apply(job: dict) -> None:
        client.create_job(job)  # type: ignore[attr-defined]

    return apply


__all__ = [
    "DispatchError",
    "TaskDispatcher",
    "kubernetes_launcher",
    "render_job",
]

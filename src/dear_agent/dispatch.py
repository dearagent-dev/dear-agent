from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass

import yaml

from dear_agent.deps import dependency_status
from dear_agent.queue.models import Task, TaskState
from dear_agent.queue.port import Queue

WORKER_ANNOTATION = "dearagent.dev/task-id"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


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


def render_job(template: str, task: Task) -> dict:
    """Render the runner JobTemplate for one task, injecting its id.

    The template is a Job manifest as a YAML string (as stored in the ConfigMap). We parse
    it, stamp the task id as an env var and an annotation, and give the Job a deterministic
    name. No shell interpolation is involved, so a hostile task id cannot inject YAML.
    """

    job = yaml.safe_load(template)
    metadata = job.setdefault("metadata", {})
    metadata.pop("generateName", None)
    metadata["name"] = job_name(task.id)
    spec = job["spec"]["template"]["spec"]
    containers = spec.get("containers", [])
    if not containers:
        raise DispatchError("runner template has no containers")
    for container in containers:
        env = container.setdefault("env", [])
        for entry in env:
            if entry.get("name") == "DEAR_AGENT_TASK_ID":
                entry["value"] = task.id
                break
        else:
            env.append({"name": "DEAR_AGENT_TASK_ID", "value": task.id})
    template_meta = job["spec"]["template"].setdefault("metadata", {})
    template_meta.setdefault("annotations", {})[WORKER_ANNOTATION] = task.id
    return job


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

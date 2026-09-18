from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import yaml

from herald.queue.models import Task, TaskState
from herald.queue.port import Queue

WORKER_ANNOTATION = "herald.dev/task-id"


class DispatchError(RuntimeError):
    """A task could not be dispatched to a runner Job."""


def render_job(template: str, task: Task) -> dict:
    """Render the runner JobTemplate for one task, injecting its id.

    The template is a Job manifest as a YAML string (as stored in the ConfigMap). We parse
    it, stamp the task id as an env var and an annotation, and return the manifest. No shell
    interpolation is involved, so a hostile task id cannot inject YAML.
    """

    job = yaml.safe_load(template)
    spec = job["spec"]["template"]["spec"]
    containers = spec.get("containers", [])
    if not containers:
        raise DispatchError("runner template has no containers")
    for container in containers:
        env = container.setdefault("env", [])
        for entry in env:
            if entry.get("name") == "HERALD_TASK_ID":
                entry["value"] = task.id
                break
        else:
            env.append({"name": "HERALD_TASK_ID", "value": task.id})
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
            job = render_job(self.template, task)
            self.launcher(job)
            created.append(task.id)
        return created

    def _already_running(self, task: Task) -> bool:
        return task.state is not TaskState.QUEUED


def kubectl_apply(namespace: str | None = None, binary: str = "oc") -> Callable[[dict], None]:
    """Build a launcher that applies a Job manifest with ``oc apply`` (fixed argv, no shell)."""

    def apply(job: dict) -> None:
        argv = [binary, "apply", "-f", "-"]
        if namespace:
            argv += ["-n", namespace]
        result = subprocess.run(
            argv,
            input=yaml.safe_dump(job),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise DispatchError(result.stderr.strip() or "kubectl apply failed")

    return apply


__all__ = ["DispatchError", "TaskDispatcher", "kubectl_apply", "render_job"]

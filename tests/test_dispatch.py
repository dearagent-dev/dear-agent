from __future__ import annotations

from datetime import timedelta

import yaml

from herald.dispatch import WORKER_ANNOTATION, TaskDispatcher, render_job
from herald.queue.memory import MemoryQueue
from herald.queue.models import Task

TEMPLATE = """
apiVersion: batch/v1
kind: Job
metadata:
  generateName: herald-task-
spec:
  template:
    spec:
      containers:
        - name: runner
          image: herald:latest
          env:
            - name: HERALD_TASK_ID
              value: ""
"""


def test_render_job_injects_the_task_id() -> None:
    task = Task(id="e1", transport_id="<m1@x>")

    job = render_job(TEMPLATE, task)

    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    assert env == [{"name": "HERALD_TASK_ID", "value": "e1"}]
    assert job["spec"]["template"]["metadata"]["annotations"][WORKER_ANNOTATION] == "e1"


def test_render_job_cannot_be_injected_by_a_hostile_task_id() -> None:
    task = Task(id="x\n  evil: true", transport_id="<m1@x>")

    job = render_job(TEMPLATE, task)

    assert job["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] == task.id
    assert "evil" not in job


def test_dispatch_creates_a_job_per_queued_task() -> None:
    queue = MemoryQueue()
    queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    queue.enqueue(Task(id="e2", transport_id="<m2@x>"))
    created: list[dict] = []

    dispatcher = TaskDispatcher(queue=queue, launcher=created.append, template=TEMPLATE)
    ids = dispatcher.dispatch_once()

    assert ids == ["e1", "e2"]
    assert len(created) == 2
    assert [j["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] for j in created] == [
        "e1",
        "e2",
    ]


def test_dispatch_is_idempotent_after_a_restart() -> None:
    queue = MemoryQueue()
    task = queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    assert task is not None
    queue.claim(task, lease=timedelta(seconds=60))
    created: list[dict] = []

    dispatcher = TaskDispatcher(queue=queue, launcher=created.append, template=TEMPLATE)

    assert dispatcher.dispatch_once() == []
    assert created == []


def test_render_job_is_valid_yaml() -> None:
    job = render_job(TEMPLATE, Task(id="e1", transport_id="<m1@x>"))

    assert yaml.safe_load(yaml.safe_dump(job)) == job

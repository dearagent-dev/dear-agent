from __future__ import annotations

import contextlib
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta

from dear_agent.deps import dependency_status
from dear_agent.events import ErrorBudget, EventLog
from dear_agent.executor import DEFAULT_LEASE, ExecutedTask, TaskExecutor
from dear_agent.policy import PolicyStore, ProjectPolicy
from dear_agent.queue.models import Task, TaskSpec, TaskState
from dear_agent.queue.port import Queue
from dear_agent.verify import CommandVerifier

SpecResolver = Callable[[Task, str], TaskSpec]

DEFAULT_MAX_ATTEMPTS = 3


class TaskWorkerError(RuntimeError):
    """A task could not be resolved or executed."""


@dataclass(slots=True)
class TaskWorker:
    """Executes one claimed task end to end.

    This is the entrypoint a runner Job invokes: given a task id (or the next queued task),
    it loads the task, resolves its content into a :class:`TaskSpec` and hands it to the
    :class:`TaskExecutor`. The spec is normally persisted with the task; the resolver only
    falls back to the mailbox for legacy records (ADR 0005).
    """

    queue: Queue
    executor: TaskExecutor
    resolve_spec: SpecResolver
    repo_path: str
    recipient: str | None = None
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    lease: timedelta = DEFAULT_LEASE
    events: EventLog | None = None
    budget: ErrorBudget = field(default_factory=ErrorBudget)
    policies: PolicyStore | None = None
    verifier: CommandVerifier | None = None

    def run(self, task_id: str) -> ExecutedTask:
        task = self.queue.get(task_id)
        if task is None:
            raise TaskWorkerError(f"no task {task_id!r} in the queue")
        self._guard_budget(task.id)
        self._guard_attempts(task)
        self._guard_dependencies(task)
        self._guard_policy(task)
        spec, verifier = self._effective(task, self.resolve_spec(task, self.repo_path))
        return self.executor.execute(task, spec, recipient=self.recipient, verifier=verifier)

    def run_next(self) -> ExecutedTask:
        """Claim and run the oldest queued task whose dependencies are satisfied."""
        self._guard_budget("<dispatch>")
        for task in self.queue.list(TaskState.QUEUED, limit=100):
            status = dependency_status(self.queue, task)
            if status.state == "failed":
                self.queue.transition(task, TaskState.FAILED)
                self._emit(task.id, "task.dependency_failed", failed_on=list(status.failed_on))
                continue
            if status.state == "blocked":
                self._emit(task.id, "task.blocked", blocked_on=list(status.blocked_on))
                continue
            try:
                self._guard_attempts(task)
                self._guard_policy(task)
            except TaskWorkerError:
                continue
            if not self.queue.claim(task, lease=self.lease):
                continue
            spec, verifier = self._effective(task, self.resolve_spec(task, self.repo_path))
            return self.executor.execute(
                task, spec, recipient=self.recipient, claimed=True, verifier=verifier
            )
        raise TaskWorkerError("no runnable task")

    def _policy_for(self, task: Task) -> ProjectPolicy | None:
        if self.policies is None or task.spec is None:
            return None
        return self.policies.for_repo(task.spec.repo_url)

    def _guard_policy(self, task: Task) -> None:
        policy = self._policy_for(task)
        if policy is not None and not policy.enabled:
            self.queue.transition(task, TaskState.FAILED)
            self._emit(task.id, "policy.denied", project=policy.project)
            raise TaskWorkerError(f"project {policy.project!r} is disabled")

    def _effective(self, task: Task, spec: TaskSpec) -> tuple[TaskSpec, CommandVerifier | None]:
        """Apply the project policy's defaults and extra verify allowlist for this task."""
        policy = self._policy_for(task)
        if policy is None:
            return spec, self.verifier
        if policy.base_branch and spec.base_branch in ("", "main"):
            spec = replace(spec, base_branch=policy.base_branch)
        verifier = self.verifier
        if policy.verify_allow:
            extra = tuple(
                tuple(shlex.split(entry)) for entry in policy.verify_allow if entry.strip()
            )
            verifier = (
                replace(verifier, allowed=verifier.allowed + extra)
                if verifier is not None
                else CommandVerifier(allowed=extra)
            )
        return spec, verifier

    def _guard_dependencies(self, task: Task) -> None:
        status = dependency_status(self.queue, task)
        if status.state == "failed":
            self.queue.transition(task, TaskState.FAILED)
            self._emit(task.id, "task.dependency_failed", failed_on=list(status.failed_on))
            raise TaskWorkerError(f"task {task.id!r} depends on failed {status.failed_on}")
        if status.state == "blocked":
            self._emit(task.id, "task.blocked", blocked_on=list(status.blocked_on))
            raise TaskWorkerError(f"task {task.id!r} is blocked on {status.blocked_on}")

    def _emit(self, task_id: str, kind: str, **data: object) -> None:
        if self.events is None:
            return
        with contextlib.suppress(Exception):
            self.events.record(task_id, kind, **data)

    def _guard_budget(self, task_id: str) -> None:
        # A durable circuit breaker: if too many tasks failed recently, stop starting new
        # runs until the window clears, rather than grinding through a systemic failure.
        if self.events is None or not self.budget.exhausted(self.events):
            return
        with contextlib.suppress(Exception):
            self.events.record(task_id, "budget.exhausted")
        raise TaskWorkerError(
            f"error budget exhausted ({self.budget.max_failures} failures in "
            f"{self.budget.window_seconds}s); not starting new runs"
        )

    def _guard_attempts(self, task: Task) -> None:
        # A task that keeps being released by the lease sweep (crash loop) must stop and ask
        # a human rather than retry forever.
        if task.attempts >= self.max_attempts:
            self.queue.transition(task, TaskState.FAILED)
            raise TaskWorkerError(
                f"task {task.id!r} exceeded {self.max_attempts} attempts; left failed"
            )


__all__ = ["DEFAULT_MAX_ATTEMPTS", "SpecResolver", "TaskWorker", "TaskWorkerError"]

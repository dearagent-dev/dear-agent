from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from herald.gitplane.plane import GitPlane
from herald.notify.notifier import Notifier, TaskLinks
from herald.queue.models import Task, TaskSpec, TaskState
from herald.queue.port import Queue
from herald.runners.port import Runner
from herald.runners.worktree import RunResult, Worktree

DEFAULT_LEASE = timedelta(hours=6)


@dataclass(slots=True)
class ExecutedTask:
    """Evidence for one task run: the PR is the deliverable, these are the links."""

    task_id: str
    branch: str
    commit: str | None
    pr_url: str | None
    run: RunResult


class TaskExecutor:
    """Runs one claimed task end to end: worktree -> harness -> commit -> push -> draft PR.

    The queue backs the state machine; the git plane owns the artifact. On success the
    task is `done` and the notifier reports the branch/commit/PR links. On harness failure
    the task is `failed` and no PR is opened. Source never leaves Git, only links travel.
    """

    def __init__(
        self,
        *,
        queue: Queue,
        runner: Runner,
        git: GitPlane,
        repo_path: str,
        worktrees_root: str,
        notifier: Notifier | None = None,
        lease: timedelta = DEFAULT_LEASE,
    ) -> None:
        self._queue = queue
        self._runner = runner
        self._git = git
        self._repo_path = repo_path
        self._worktrees_root = worktrees_root
        self._notifier = notifier
        self._lease = lease

    def execute(self, task: Task, spec: TaskSpec, *, recipient: str | None = None) -> ExecutedTask:
        if not self._queue.claim(task, lease=self._lease):
            raise RuntimeError(f"task {task.id} is not claimable")
        running = self._queue.get(task.id)
        assert running is not None

        worktree = Worktree.create(
            repo_path=self._repo_path,
            worktrees_root=self._worktrees_root,
            slug=_slug(task),
            base_branch=spec.base_branch,
        )
        try:
            run = self._runner.run(task, spec, worktree)

            commit: str | None = None
            pr_url: str | None = None
            if run.ok:
                commit = self._git.commit_all(worktree, message=_commit_message(task, spec))
                self._git.push(worktree)
                pr = self._git.open_draft_pr(
                    worktree,
                    base_branch=spec.base_branch,
                    title=_pr_title(task),
                    body=_pr_body(task),
                )
                pr_url = pr.url

            final = self._queue.transition(running, TaskState.DONE if run.ok else TaskState.FAILED)
            evidence = ExecutedTask(
                task_id=final.id,
                branch=worktree.branch,
                commit=commit,
                pr_url=pr_url,
                run=run,
            )
            self._notify(evidence, recipient=recipient)
            return evidence
        finally:
            worktree.remove()

    def _notify(self, evidence: ExecutedTask, *, recipient: str | None) -> None:
        if self._notifier is None or recipient is None:
            return
        task = self._queue.get(evidence.task_id)
        if task is None:
            return
        summary = "completed" if evidence.run.ok else "harness failed"
        self._notifier.status(
            task,
            recipient=recipient,
            summary=summary,
            links=TaskLinks(
                branch=evidence.branch,
                commit=evidence.commit,
                pr_url=evidence.pr_url,
            ),
        )


def _slug(task: Task) -> str:
    raw = task.subject or task.id
    slug = "".join(char if char.isalnum() else "-" for char in raw.lower()).strip("-")
    return "-".join(filter(None, slug.split("-")))[:40] or "task"


def _commit_message(task: Task, spec: TaskSpec) -> str:
    return (
        task.subject
        or f"herald: {spec.instructions.splitlines()[0] if spec.instructions else task.id}"
    )


def _pr_title(task: Task) -> str:
    return task.subject or f"herald: {task.id}"


def _pr_body(task: Task) -> str:
    return "\n".join(
        [
            f"Task: `{task.id}`",
            "",
            "Opened by an agent. A human reviews and lands it; agents never write `main`.",
        ]
    )


__all__ = ["ExecutedTask", "TaskExecutor"]

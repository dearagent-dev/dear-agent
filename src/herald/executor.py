from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from herald.gitplane.plane import GitError, GitPlane
from herald.notify.escalate import Escalator, FailureKind
from herald.notify.notifier import Notifier, TaskLinks
from herald.queue.models import Evidence, Task, TaskSpec, TaskState
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
    failure: FailureKind | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


class TaskExecutor:
    """Runs one claimed task end to end: worktree -> harness -> commit -> push -> draft PR.

    The queue backs the state machine; the git plane owns the artifact. On success the
    task is `done` and the notifier reports the branch/commit/PR links. A failed or
    no-change run marks the task `failed`, opens no PR and escalates to a human. Source
    never leaves Git; only links travel.
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
        escalator: Escalator | None = None,
        lease: timedelta = DEFAULT_LEASE,
    ) -> None:
        self._queue = queue
        self._runner = runner
        self._git = git
        self._repo_path = repo_path
        self._worktrees_root = worktrees_root
        self._notifier = notifier
        self._escalator = escalator
        self._lease = lease

    def execute(
        self,
        task: Task,
        spec: TaskSpec,
        *,
        recipient: str | None = None,
        claimed: bool = False,
    ) -> ExecutedTask:
        """Run ``task`` end to end.

        Set ``claimed=True`` when the caller already claimed the task atomically (e.g.
        :meth:`~herald.queue.port.Queue.claim_next`), so it is not claimed twice.
        """
        if not claimed and not self._queue.claim(task, lease=self._lease):
            raise RuntimeError(f"task {task.id} is not claimable")
        running = self._queue.get(task.id)
        assert running is not None

        try:
            worktree = Worktree.create(
                repo_path=self._repo_path,
                worktrees_root=self._worktrees_root,
                slug=_slug(task),
                base_branch=spec.base_branch,
            )
        except Exception:
            # A worktree that cannot be created (bad worktrees root, unwritable dir) must
            # fail the task, not leave it running until the lease expires.
            self._queue.transition(running, TaskState.FAILED)
            raise
        try:
            try:
                run = self._runner.run(task, spec, worktree)
            except Exception as exc:  # noqa: BLE001 - a broken runner must not wedge the task
                run = RunResult(
                    exit_code=1,
                    stderr=f"runner raised {type(exc).__name__}",
                    branch=worktree.branch,
                )
            failure: FailureKind | None = None
            commit: str | None = None
            pr_url: str | None = None

            if run.ok:
                # The harness succeeded; a git/forge error here must fail the task cleanly,
                # never leave it `running` until the lease expires.
                try:
                    commit = self._git.commit_all(worktree, message=_commit_message(task, spec))
                    if not self._git.has_changes_since(worktree, spec.base_branch):
                        failure = FailureKind.NO_CHANGES
                    else:
                        self._git.push(worktree)
                        pr = self._git.open_draft_pr(
                            worktree,
                            base_branch=spec.base_branch,
                            title=_pr_title(task),
                            body=_pr_body(task),
                        )
                        pr_url = pr.url
                except GitError:
                    failure = FailureKind.PUBLISH_FAILED
                    pr_url = None
            else:
                failure = (
                    FailureKind.HARNESS_MISSING
                    if run.exit_code == 127
                    else (
                        FailureKind.TIMEOUT if run.exit_code == 124 else FailureKind.HARNESS_FAILED
                    )
                )

            evidence = ExecutedTask(
                task_id=running.id,
                branch=worktree.branch,
                commit=commit,
                pr_url=pr_url,
                run=run,
                failure=failure,
            )
            final = self._queue.transition(
                running,
                TaskState.DONE if evidence.ok else TaskState.FAILED,
                evidence=Evidence(
                    branch=evidence.branch,
                    commit=evidence.commit,
                    pr_url=evidence.pr_url,
                ),
            )
            evidence.task_id = final.id
            self._report(evidence, recipient=recipient)
            return evidence
        finally:
            worktree.remove()

    def _report(self, evidence: ExecutedTask, *, recipient: str | None) -> None:
        if not recipient:
            return
        task = self._queue.get(evidence.task_id)
        if task is None:
            return
        if evidence.failure is not None and self._escalator is not None:
            self._escalator.escalate(
                task,
                evidence.run,
                recipient=recipient,
                branch=evidence.branch,
                kind=evidence.failure,
            )
            return
        if self._notifier is None:
            return
        self._notifier.status(
            task,
            recipient=recipient,
            summary="completed" if evidence.ok else "harness failed",
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

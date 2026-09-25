from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

from dear_agent.events import EventLog
from dear_agent.gitplane.plane import GitError, GitPlane
from dear_agent.notify.escalate import Escalator, FailureKind
from dear_agent.notify.notifier import Notifier, TaskLinks
from dear_agent.queue.models import Evidence, Task, TaskSpec, TaskState
from dear_agent.queue.port import Queue
from dear_agent.review import Reviewer, ReviewVerdict
from dear_agent.runners.port import Runner, SessionProvider
from dear_agent.runners.worktree import RunResult, Worktree
from dear_agent.sandbox import Sandbox
from dear_agent.verify import CommandVerifier

DEFAULT_LEASE = timedelta(hours=6)
_VERIFY_FAILURES = frozenset({FailureKind.VERIFY_FAILED, FailureKind.VERIFY_BLOCKED})


@dataclass(slots=True)
class ExecutedTask:
    """Evidence for one task run: the PR is the deliverable, these are the links."""

    task_id: str
    branch: str
    commit: str | None
    pr_url: str | None
    run: RunResult
    failure: FailureKind | None = None
    detail: str | None = None
    review: ReviewVerdict | None = None

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
        verifier: CommandVerifier | None = None,
        events: EventLog | None = None,
        lease: timedelta = DEFAULT_LEASE,
        session: Sandbox | None = None,
        reviewer: Reviewer | None = None,
        debate_rounds: int = 0,
    ) -> None:
        self._queue = queue
        self._runner = runner
        self._git = git
        self._repo_path = repo_path
        self._worktrees_root = worktrees_root
        self._notifier = notifier
        self._escalator = escalator
        self._verifier = verifier
        self._events = events
        self._lease = lease
        # The environment session the runner and the verifier share (ADR 0008). The executor
        # owns its lifetime, so a failed run never leaks a container.
        self._session = session
        # An optional adversarial reviewer of the diff, bounded to `debate_rounds` revisions
        # (ADR 0010). Advisory and fail-open.
        self._reviewer = reviewer
        self._debate_rounds = debate_rounds

    def _emit(self, task_id: str, kind: str, **data: object) -> None:
        if self._events is None:
            return
        try:
            self._events.record(task_id, kind, **data)
        except Exception:  # noqa: BLE001 - events are best effort
            return

    def execute(
        self,
        task: Task,
        spec: TaskSpec,
        *,
        recipient: str | None = None,
        claimed: bool = False,
        verifier: CommandVerifier | None = None,
    ) -> ExecutedTask:
        """Run ``task`` end to end.

        Set ``claimed=True`` when the caller already claimed the task atomically (e.g.
        :meth:`~dear_agent.queue.port.Queue.claim_next`), so it is not claimed twice.
        """
        if not claimed and not self._queue.claim(task, lease=self._lease):
            raise RuntimeError(f"task {task.id} is not claimable")
        running = self._queue.get(task.id)
        assert running is not None
        self._emit(task.id, "task.claimed")

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
        run_session: Sandbox | None = None
        try:
            # A routing runner chooses the harness (and therefore the image) per task; the
            # verify gate must run in that same session (ADR 0008).
            active_verifier = verifier or self._verifier
            if isinstance(self._runner, SessionProvider):
                run_session = self._runner.session_for(task, spec)
                if run_session is not None and active_verifier is not None:
                    active_verifier = replace(active_verifier, sandbox=run_session)
            review: ReviewVerdict | None = None
            run = self._run_harness(task, spec, worktree)
            failure: FailureKind | None = None
            commit: str | None = None
            pr_url: str | None = None

            if run.ok:
                # The harness succeeded. Run the task's verify gate first (only if the
                # command is allowlisted), then an optional adversarial review (ADR 0010), then
                # publish. A git/forge error must fail the task cleanly, never leave it
                # `running` until the lease expires.
                failure = self._verify(task, spec, worktree, active_verifier)
                if failure is None:
                    run, spec, failure, review = self._debate(
                        task, spec, worktree, run, failure, active_verifier
                    )
                if failure is None:
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
                                body=_pr_body(task, review),
                            )
                            pr_url = pr.url
                    except GitError:
                        failure = FailureKind.PUBLISH_FAILED
                        pr_url = None
                        self._emit(task.id, "publish.failed")
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
                detail=spec.verify if failure in _VERIFY_FAILURES else None,
                review=review,
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
            self._emit(
                final.id,
                "task.done" if evidence.ok else "task.failed",
                failure=evidence.failure.value if evidence.failure else None,
                pr_url=evidence.pr_url,
            )
            self._report(evidence, recipient=recipient)
            return evidence
        finally:
            # End the environment session(s) before the worktree disappears, so the harness's
            # provisioning cannot outlive the task (ADR 0008). A routing runner's session can
            # differ from the one the factory built.
            if run_session is not None and run_session is not self._session:
                run_session.close()
            if self._session is not None:
                self._session.close()
            worktree.remove()

    def _verify(
        self,
        task: Task,
        spec: TaskSpec,
        worktree: Worktree,
        verifier: CommandVerifier | None = None,
    ) -> FailureKind | None:
        """Run the task's verify gate, if any. Returns a failure kind, or ``None`` to pass."""
        if not spec.verify:
            return None
        active = verifier or self._verifier
        if active is None:
            self._emit(task.id, "verify.blocked", command=spec.verify)
            return FailureKind.VERIFY_BLOCKED
        try:
            result = active.run(spec.verify, str(worktree.path))
        except Exception:  # noqa: BLE001 - a broken verifier must fail the task, not wedge it
            self._emit(task.id, "verify.failed", command=spec.verify, error="verifier raised")
            return FailureKind.VERIFY_FAILED
        if result.blocked:
            self._emit(task.id, "verify.blocked", command=spec.verify)
            return FailureKind.VERIFY_BLOCKED
        if not result.ok:
            self._emit(task.id, "verify.failed", command=spec.verify, exit_code=result.exit_code)
            return FailureKind.VERIFY_FAILED
        self._emit(task.id, "verify.passed", command=spec.verify)
        return None

    def _run_harness(self, task: Task, spec: TaskSpec, worktree: Worktree) -> RunResult:
        try:
            return self._runner.run(task, spec, worktree)
        except Exception as exc:  # noqa: BLE001 - a broken runner must not wedge the task
            return RunResult(
                exit_code=1,
                stderr=f"runner raised {type(exc).__name__}",
                branch=worktree.branch,
            )

    def _debate(
        self,
        task: Task,
        spec: TaskSpec,
        worktree: Worktree,
        run: RunResult,
        failure: FailureKind | None,
        verifier: CommandVerifier | None,
    ) -> tuple[RunResult, TaskSpec, FailureKind | None, ReviewVerdict | None]:
        """Let a second model review the diff; on "revise", re-run the harness (ADR 0010).

        Bounded to ``debate_rounds`` revisions, advisory and fail-open: no reviewer, a review
        error, or a re-run that fails leaves the prior change to be published.
        """
        if self._reviewer is None or self._debate_rounds <= 0:
            return run, spec, failure, None
        review: ReviewVerdict | None = None
        remaining = self._debate_rounds
        while True:
            try:
                diff = self._git.diff(worktree, spec.base_branch)
                verdict = self._reviewer.review(diff=diff, instructions=spec.instructions)
            except Exception:  # noqa: BLE001 - the reviewer is advisory; never wedge the task
                self._emit(task.id, "review.skipped")
                return run, spec, failure, review
            review = verdict
            self._emit(task.id, "review.approved" if verdict.approved else "review.revise")
            if verdict.approved or remaining <= 0:
                return run, spec, failure, review
            remaining -= 1
            revised = replace(
                spec, instructions=_revise_instructions(spec.instructions, verdict.notes)
            )
            next_run = self._run_harness(task, revised, worktree)
            if not next_run.ok:
                return run, spec, failure, review
            next_failure = self._verify(task, revised, worktree, verifier)
            run, spec, failure = next_run, revised, next_failure
            if next_failure is not None:
                return run, spec, failure, review

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
                detail=evidence.detail,
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
        or f"dear-agent: {spec.instructions.splitlines()[0] if spec.instructions else task.id}"
    )


def _pr_title(task: Task) -> str:
    return task.subject or f"dear-agent: {task.id}"


def _pr_body(task: Task, review: ReviewVerdict | None = None) -> str:
    lines = [
        f"Task: `{task.id}`",
        "",
        "Opened by an agent. A human reviews and lands it; agents never write `main`.",
    ]
    if review is not None:
        lines.append("")
        lines.append(f"Reviewer: {'approved' if review.approved else 'requested a revision'}.")
        if review.summary:
            lines.append(f"_{review.summary}_")
    return "\n".join(lines)


def _revise_instructions(instructions: str, notes: str) -> str:
    if not notes.strip():
        return instructions
    return (
        f"{instructions}\n\n---\nA reviewer asked for a revision. Address these points:\n"
        f"{notes.strip()}"
    )


__all__ = ["ExecutedTask", "TaskExecutor"]

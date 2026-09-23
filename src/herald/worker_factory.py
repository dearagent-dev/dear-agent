from __future__ import annotations

import os

from herald.executor import TaskExecutor
from herald.gitplane.gh import GhForge
from herald.gitplane.plane import GitPlane
from herald.notify.escalate import Escalator
from herald.notify.notifier import Notifier
from herald.queue.models import Task, TaskSpec
from herald.queue.port import Queue
from herald.repo import RepoPreparer
from herald.runners.port import Runner
from herald.sandbox import BubblewrapSandbox, NoSandbox, Sandbox, SandboxPolicy
from herald.transports.port import Transport
from herald.worker import DEFAULT_MAX_ATTEMPTS, TaskWorker


class WorktreeSpecResolver:
    """Resolves a ``TaskSpec`` by parsing the message body via the Normalizer.

    The queue record does not carry the content (ADR 0002), so the worker re-reads the
    message and normalizes it. A task with no resolvable spec is a hard error: the runner
    must not invent instructions. After resolving, it ensures the read-only checkout exists
    so the executor can create a worktree from it.
    """

    def __init__(self, transport: Transport, preparer: RepoPreparer | None = None) -> None:
        self._transport = transport
        self._preparer = preparer or RepoPreparer()

    def __call__(self, task: Task, repo_path: str) -> TaskSpec:
        # The spec is persisted with the task (ADR 0005), so a runner needs no mailbox. Fall
        # back to re-parsing the message only for tasks enqueued before that.
        spec = task.spec or self._from_transport(task)
        if spec.repo_url:
            self._preparer.ensure(repo_path, spec.repo_url)
        return spec

    def _from_transport(self, task: Task) -> TaskSpec:
        from herald.normalizer import NormalizedTask, normalize

        message = next(
            (raw for raw in self._transport.poll() if raw.transport_id == task.transport_id),
            None,
        )
        if message is None:
            raise LookupError(f"message {task.transport_id!r} not found in the transport")
        result = normalize(message)
        if not isinstance(result, NormalizedTask):
            raise ValueError(f"message {task.transport_id!r} does not normalize: {result.reason}")
        return result.spec


def build_runner(provider_model: str | None, sandbox: Sandbox) -> Runner:
    """Build the harness runner selected by ``HERALD_HARNESS`` (default: opencode).

    ``opencode`` (default), ``claude`` and ``codex`` are first-class adapters; ``auto`` /
    ``local-agent`` picks the first one available on ``PATH``. ``command`` wraps an arbitrary
    harness via ``HERALD_HARNESS_COMMAND`` (a space-separated argv), which is how an operator
    plugs in a custom runner without forking Herald. A model is passed only to harnesses that
    accept one (OpenCode), never to a subscription harness (Claude Code, Codex).
    """
    from dataclasses import replace

    from herald.runners.catalog import EnvHarnessCatalog, build_runner_for, select_harness

    info = select_harness(EnvHarnessCatalog(), os.environ.get("HERALD_HARNESS"))
    override = os.environ.get("HERALD_HARNESS_BINARY")
    if override and info.id != "command":
        info = replace(info, binary=override)
    return build_runner_for(info, provider_model, sandbox)


def default_worktrees_root() -> str:
    """Where disposable worktrees live.

    ``HERALD_WORKTREES_ROOT`` wins (the cluster sets ``/work``); otherwise a writable temp
    directory, so a local run works without configuration.
    """
    import tempfile
    from pathlib import Path

    return os.environ.get(
        "HERALD_WORKTREES_ROOT", str(Path(tempfile.gettempdir()) / "herald-worktrees")
    )


def default_sandbox() -> Sandbox:
    """Use bubblewrap when available, otherwise run unsandboxed (explicitly)."""
    if os.environ.get("HERALD_SANDBOX", "bwrap") == "bwrap":
        candidate = BubblewrapSandbox(policy=SandboxPolicy())
        if candidate.available:
            return candidate
    return NoSandbox()


def build_routing_runner(sandbox: Sandbox) -> Runner | None:
    """Build a per-task routing runner from ``HERALD_HARNESSES``, or ``None``.

    ``HERALD_HARNESSES`` maps routing classes to harnesses, e.g. ``local:opencode,hosted:claude``.
    When set, every run asks the decider which class fits the task and dispatches to that
    harness; the default class is ``HERALD_HARNESS_DEFAULT`` (default ``hosted``). When unset,
    routing is off and the single ``HERALD_HARNESS`` runner is used.
    """
    spec = os.environ.get("HERALD_HARNESSES")
    if not spec:
        return None
    from herald.runners.routing import RoutingRunner

    runners: dict[str, Runner] = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        class_name, _, harness = entry.partition(":")
        harness = harness or class_name
        saved = os.environ.get("HERALD_HARNESS")
        os.environ["HERALD_HARNESS"] = harness
        try:
            runners[class_name.strip()] = build_runner(None, sandbox)
        finally:
            if saved is None:
                os.environ.pop("HERALD_HARNESS", None)
            else:
                os.environ["HERALD_HARNESS"] = saved

    from herald.decision.factory import build_decider

    return RoutingRunner(
        runners=runners,
        decider=build_decider(),
        default=os.environ.get("HERALD_HARNESS_DEFAULT", "hosted"),
    )


def build_transport(backend: str | None = None) -> Transport:
    """Build the inbound/outbound transport.

    The backend comes from the ``--backend`` flag when given, otherwise
    ``HERALD_BACKEND`` (default: memory). Passing the CLI value matters: the runner is
    invoked as ``herald run --backend jmap`` and its spec resolver must poll the same
    transport the task came from.
    """
    backend = backend or os.environ.get("HERALD_BACKEND", "memory")
    if backend == "memory":
        from herald.transports.memory import MemoryTransport

        return MemoryTransport()
    if backend == "jmap":
        from herald.jmap.client import DEFAULT_SESSION_URL, JmapClient
        from herald.transports.jmap import JmapTransport

        token = os.environ.get("FASTMAIL_API_TOKEN")
        if not token:
            raise RuntimeError("FASTMAIL_API_TOKEN is required for the jmap backend")
        client = JmapClient(
            token,
            account_id=os.environ.get("FASTMAIL_ACCOUNT_ID"),
            session_url=os.environ.get("FASTMAIL_SESSION_URL", DEFAULT_SESSION_URL),
        )
        client.connect()
        return JmapTransport(client, mailbox_name=os.environ.get("HERALD_MAILBOX", "Herald"))
    if backend == "imap":
        from herald.imap.client import ImapClient
        from herald.smtp.client import SmtpClient
        from herald.transports.imap import ImapSmtpTransport

        host = os.environ.get("HERALD_IMAP_HOST")
        if not host:
            raise RuntimeError("HERALD_IMAP_HOST is required for the imap backend")
        user = os.environ.get("HERALD_IMAP_USER", "")
        password = os.environ.get("HERALD_IMAP_PASSWORD", "")
        imap = ImapClient(
            host=host,
            user=user,
            password=password,
            port=int(os.environ.get("HERALD_IMAP_PORT", "993")),
            ssl=os.environ.get("HERALD_IMAP_SSL", "true").lower() != "false",
        )
        smtp = SmtpClient(
            host=os.environ.get("HERALD_SMTP_HOST", host),
            port=int(os.environ.get("HERALD_SMTP_PORT", "587")),
            user=os.environ.get("HERALD_SMTP_USER", user),
            password=os.environ.get("HERALD_SMTP_PASSWORD", password),
            starttls=os.environ.get("HERALD_SMTP_STARTTLS", "true").lower() != "false",
            ssl=os.environ.get("HERALD_SMTP_SSL", "false").lower() == "true",
        )
        return ImapSmtpTransport(
            imap=imap,
            smtp=smtp,
            mailbox=os.environ.get("HERALD_IMAP_MAILBOX", "INBOX"),
            done_mailbox=os.environ.get("HERALD_IMAP_DONE_MAILBOX", "Herald-Done"),
            sender=os.environ.get("HERALD_SMTP_FROM", user),
        )
    raise RuntimeError(f"unknown backend {backend!r}")


def build_worker(
    *,
    queue: Queue,
    transport: Transport,
    repo_path: str,
    recipient: str | None,
    provider_model: str | None,
    sandbox: Sandbox | None = None,
) -> TaskWorker:
    """Wire a TaskWorker from its collaborators."""
    from herald.approvals import build_approval_store
    from herald.approvals_service import ApprovalService
    from herald.events import ErrorBudget, build_event_log
    from herald.verify import CommandVerifier

    sandbox = sandbox or default_sandbox()
    runner = build_routing_runner(sandbox)
    if runner is None:
        runner = build_runner(provider_model, sandbox)
    approvals = ApprovalService(build_approval_store(), queue)
    events = build_event_log()
    executor = TaskExecutor(
        queue=queue,
        runner=runner,
        git=GitPlane(forge=GhForge()),
        repo_path=repo_path,
        worktrees_root=default_worktrees_root(),
        notifier=Notifier(transport, approvals=approvals),
        escalator=Escalator(transport),
        verifier=CommandVerifier.from_env(os.environ),
        events=events,
    )
    return TaskWorker(
        queue=queue,
        executor=executor,
        resolve_spec=WorktreeSpecResolver(transport),
        repo_path=repo_path,
        recipient=recipient,
        max_attempts=int(os.environ.get("HERALD_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)),
        events=events,
        budget=ErrorBudget.from_env(os.environ),
    )


__all__ = [
    "WorktreeSpecResolver",
    "build_runner",
    "build_worker",
    "default_sandbox",
    "default_worktrees_root",
]

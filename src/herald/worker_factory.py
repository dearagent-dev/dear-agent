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
from herald.worker import TaskWorker


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
        if result.spec.repo_url:
            self._preparer.ensure(repo_path, result.spec.repo_url)
        return result.spec


def build_runner(provider_model: str | None, sandbox: Sandbox) -> Runner:
    """Build the OpenCode runner with the given sandbox."""
    from herald.runners.opencode import OpenCodeRunner

    binary = os.environ.get("HERALD_HARNESS_BINARY", "opencode")
    return OpenCodeRunner(model=provider_model, binary=binary, sandbox=sandbox)


def default_sandbox() -> Sandbox:
    """Use bubblewrap when available, otherwise run unsandboxed (explicitly)."""
    if os.environ.get("HERALD_SANDBOX", "bwrap") == "bwrap":
        candidate = BubblewrapSandbox(policy=SandboxPolicy())
        if candidate.available:
            return candidate
    return NoSandbox()


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
    runner = build_runner(provider_model, sandbox or default_sandbox())
    executor = TaskExecutor(
        queue=queue,
        runner=runner,
        git=GitPlane(forge=GhForge()),
        repo_path=repo_path,
        worktrees_root=os.environ.get("HERALD_WORKTREES_ROOT", "/work"),
        notifier=Notifier(transport),
        escalator=Escalator(transport),
    )
    return TaskWorker(
        queue=queue,
        executor=executor,
        resolve_spec=WorktreeSpecResolver(transport),
        repo_path=repo_path,
        recipient=recipient,
    )


__all__ = ["WorktreeSpecResolver", "build_runner", "build_worker", "default_sandbox"]

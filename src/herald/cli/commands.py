from __future__ import annotations

import argparse
import os
from datetime import timedelta
from typing import Any

from herald.cli.context import CliContext
from herald.queue.models import Task, TaskSpec, TaskState

LEASE_SECONDS_DEFAULT = 3600


def _task_payload(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "transport_id": task.transport_id,
        "thread_id": task.thread_id,
        "sender": task.sender,
        "subject": task.subject,
        "state": task.state.value,
        "attempts": task.attempts,
        "lease_until": task.lease_until,
        "created_at": task.created_at,
        "repo": task.spec.repo_url if task.spec else None,
        "base": task.spec.base_branch if task.spec else None,
        "model": task.spec.model_request if task.spec else None,
        "branch": task.evidence.branch if task.evidence else None,
        "commit": task.evidence.commit if task.evidence else None,
        "pr_url": task.evidence.pr_url if task.evidence else None,
    }


def _print_tasks(context: CliContext, tasks: list[Task]) -> None:
    if context.as_json:
        context.emit_json([_task_payload(task) for task in tasks])
        return
    if not tasks:
        context.emit("no tasks")
        return
    context.emit(f"{'ID':<28} {'STATE':<9} {'ATT':<4} SUBJECT")
    for task in tasks:
        context.emit(f"{task.id:<28} {task.state.value:<9} {task.attempts:<4} {task.subject or ''}")


def _add_list(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser("ls", help="list tasks")
    parser.add_argument(
        "--state",
        choices=[state.value for state in TaskState],
        default=TaskState.QUEUED.value,
        help="filter by state (default: queued)",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.set_defaults(handler=_handle_list)


def _handle_list(args: argparse.Namespace, context: CliContext) -> int:
    _print_tasks(context, context.require_queue().list(TaskState(args.state), limit=args.limit))
    return 0


def _add_show(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("show", help="show one task")
    parser.add_argument("task_id")
    parser.set_defaults(handler=_handle_show)


def _handle_show(args: argparse.Namespace, context: CliContext) -> int:
    task = context.require_queue().get(args.task_id)
    if task is None:
        context.emit(f"no task {args.task_id!r}")
        return 1
    if context.as_json:
        context.emit_json(_task_payload(task))
    else:
        for key, value in _task_payload(task).items():
            context.emit(f"{key}: {value}")
    return 0


def _add_enqueue(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "enqueue", help="enqueue a task directly (local/testing; the email path is separate)"
    )
    parser.add_argument("instructions", help="what the harness should do")
    parser.add_argument("--repo", required=True, help="repository URL (or owner/repo)")
    parser.add_argument("--base", default="main", help="base branch (default: main)")
    parser.add_argument("--subject", default=None, help="task subject (default: from instructions)")
    parser.add_argument("--model", default=None, help="provider:model hint for the harness")
    parser.add_argument("--transport-id", default=None, help="dedupe key (default: generated)")
    parser.set_defaults(handler=_handle_enqueue)


def _handle_enqueue(args: argparse.Namespace, context: CliContext) -> int:
    import uuid

    transport_id = args.transport_id or f"<manual-{uuid.uuid4().hex}@herald.local>"
    task = Task(
        id=transport_id,
        transport_id=transport_id,
        subject=args.subject or args.instructions.splitlines()[0][:80],
        spec=TaskSpec(
            repo_url=args.repo,
            base_branch=args.base,
            instructions=args.instructions,
            model_request=args.model,
        ),
    )
    stored = context.require_queue().enqueue(task)
    if stored is None:
        context.emit(f"task already exists for {transport_id}")
        return 1
    if context.as_json:
        context.emit_json(_task_payload(stored))
    else:
        context.emit(f"enqueued {stored.id}")
    return 0


def _add_claim(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("claim", help="claim a queued task (queued -> running)")
    parser.add_argument("task_id")
    parser.add_argument("--lease-seconds", type=int, default=LEASE_SECONDS_DEFAULT)
    parser.set_defaults(handler=_handle_claim)


def _handle_claim(args: argparse.Namespace, context: CliContext) -> int:
    task = context.require_queue().get(args.task_id)
    if task is None:
        context.emit(f"no task {args.task_id!r}")
        return 1
    if not context.require_queue().claim(task, lease=timedelta(seconds=args.lease_seconds)):
        context.emit(f"task {task.id} is not claimable (state {task.state.value})")
        return 1
    context.emit(f"claimed {task.id} for {args.lease_seconds}s")
    return 0


def _add_terminal(
    subparsers: argparse._SubParsersAction, name: str, target: TaskState, help_text: str
) -> None:
    parser = subparsers.add_parser(name, help=help_text)
    parser.add_argument("task_id")
    parser.set_defaults(handler=_handle_transition, target_state=target)


def _handle_transition(args: argparse.Namespace, context: CliContext) -> int:
    task = context.require_queue().get(args.task_id)
    if task is None:
        context.emit(f"no task {args.task_id!r}")
        return 1
    transitioned = context.require_queue().transition(task, args.target_state)
    context.emit(f"{transitioned.id} -> {transitioned.state.value}")
    return 0


def add_task_commands(
    subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser
) -> None:
    task = subparsers.add_parser("task", help="manage tasks")
    task.set_defaults(handler=None)
    task_sub = task.add_subparsers(dest="task_command", required=True)

    _add_list(task_sub, task)
    _add_show(task_sub)
    _add_enqueue(task_sub)
    _add_claim(task_sub)
    _add_terminal(task_sub, "complete", TaskState.DONE, "mark a running task done")
    _add_terminal(task_sub, "fail", TaskState.FAILED, "mark a running task failed")
    _add_terminal(task_sub, "requeue", TaskState.QUEUED, "return a failed/rejected task to queued")
    _add_terminal(task_sub, "cancel", TaskState.REJECTED, "reject a task without running it")


def add_sweep_commands(
    subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "sweep", help="release stale runs and dispatch queued tasks to runner Jobs"
    )
    parser.add_argument("--template", default=os.environ.get("HERALD_RUNNER_TEMPLATE_CONFIGMAP"))
    parser.add_argument("--limit", type=int, default=20)
    parser.set_defaults(handler=_handle_sweep)


def _handle_sweep(args: argparse.Namespace, context: CliContext) -> int:
    from datetime import UTC, datetime

    from herald.dispatch import TaskDispatcher, kubernetes_launcher
    from herald.kube import KubernetesClient, KubernetesError

    queue = context.require_queue()
    released = queue.release_stale(now=datetime.now(UTC))

    if not args.template:
        raise RuntimeError("HERALD_RUNNER_TEMPLATE_CONFIGMAP is required to dispatch")
    try:
        client = KubernetesClient.from_cluster()
    except KubernetesError as exc:
        raise RuntimeError(
            f"cannot reach the Kubernetes API ({exc}); sweep must run in-cluster"
        ) from exc
    template = client.get_configmap(args.template).get("job.yaml")
    if not template:
        raise RuntimeError(f"configmap {args.template!r} has no job.yaml key")
    dispatcher = TaskDispatcher(
        queue=queue,
        launcher=kubernetes_launcher(client),
        template=template,
        limit=args.limit,
    )
    created = dispatcher.dispatch_once()

    payload = {"released": [task.id for task in released], "dispatched": created}
    if context.as_json:
        context.emit_json(payload)
    else:
        context.emit(f"released {len(released)} stale, dispatched {len(created)}")
    return 0


def add_listen_commands(
    subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "listen", help="ingest on JMAP push events (EventSource) instead of polling"
    )
    parser.set_defaults(handler=_handle_listen)


def _handle_listen(args: argparse.Namespace, context: CliContext) -> int:
    import os

    from herald.jmap.client import DEFAULT_SESSION_URL, JmapClient
    from herald.jmap.eventsource import EventSourceListener
    from herald.jmap.trigger import EventSourceLoop, IngestOnChange
    from herald.worker_factory import build_transport

    token = os.environ.get("FASTMAIL_API_TOKEN")
    if not token:
        raise RuntimeError("FASTMAIL_API_TOKEN is required for listen")
    client = JmapClient(
        token,
        account_id=os.environ.get("FASTMAIL_ACCOUNT_ID"),
        session_url=os.environ.get("FASTMAIL_SESSION_URL", DEFAULT_SESSION_URL),
    )
    client.connect()

    queue = context.require_queue()
    transport = build_transport("jmap")
    recipient = os.environ.get("HERALD_RECIPIENT")
    callback = IngestOnChange(
        control_plane=_inbound_plane(queue, transport),
        transport=transport,
        recipient=recipient,
    )
    loop = EventSourceLoop(
        listener=EventSourceListener(
            url=client.event_source_url,
            on_event=callback,
            token=token,
        ),
        callback=callback,
    )
    loop.run()
    return 0


def _inbound_plane(queue, transport):
    """Build the ControlPlane for an inbound path (webhook/poll/listen).

    Wires the notifier, the SPF/DKIM/DMARC gate (or a sender allowlist as a weaker
    fallback), and approvals, so rejections are explained and approval replies are applied.
    Email cannot carry Herald's HMAC header, so the transport-level gate is what
    authenticates it.
    """
    from herald.approvals import build_approval_store
    from herald.approvals_service import ApprovalService
    from herald.auth import EmailAuthGate, SenderAllowlist
    from herald.control_plane import ControlPlane
    from herald.notify.notifier import Notifier

    gate = EmailAuthGate.from_env(os.environ)
    if gate is None:
        gate = SenderAllowlist.from_env(os.environ.get("HERALD_ALLOWED_SENDERS"))
    return ControlPlane(
        transport=transport,
        queue=queue,
        notifier=Notifier(transport),
        gate=gate,
        approvals=ApprovalService(build_approval_store(), queue),
    )


def add_poll_commands(
    subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "poll", help="run one inbound ingest pass over the configured pull transport"
    )
    parser.add_argument(
        "--recipient", default=None, help="reply recipient (default: HERALD_RECIPIENT)"
    )
    parser.set_defaults(handler=_handle_poll)


def _handle_poll(args: argparse.Namespace, context: CliContext) -> int:
    from herald.worker_factory import build_transport

    queue = context.require_queue()
    transport = build_transport()
    recipient = args.recipient or os.environ.get("HERALD_RECIPIENT")
    report = _inbound_plane(queue, transport).ingest(transport.poll(), recipient=recipient)
    payload = {
        "accepted": report.accepted,
        "rejected": report.rejected,
        "denied": report.denied,
        "decided": report.decided,
        "suspicious": report.suspicious,
    }
    if context.as_json:
        context.emit_json(payload)
    else:
        context.emit(
            f"accepted={len(report.accepted)} rejected={len(report.rejected)} "
            f"denied={len(report.denied)}"
        )
    return 0


def add_idle_commands(
    subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "idle", help="propose work from recent repo activity when the queue is empty"
    )
    parser.add_argument("--repo", required=True, help="path to a local checkout to read")
    parser.add_argument(
        "--repo-url", default=None, help="repo URL for tasks (default: the --repo path)"
    )
    parser.add_argument("--max", type=int, default=1, help="max proposals per tick (default: 1)")
    parser.add_argument(
        "--min-queue-depth",
        type=int,
        default=1,
        help="only propose when fewer than N tasks are queued (default: 1)",
    )
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="enqueue proposals as runnable tasks instead of parking them for approval",
    )
    parser.add_argument(
        "--recipient",
        default=None,
        help="email to send approval requests to (default: HERALD_RECIPIENT)",
    )
    parser.set_defaults(handler=_handle_idle)


def _handle_idle(args: argparse.Namespace, context: CliContext) -> int:
    import hashlib
    from pathlib import Path

    from herald.approvals import build_approval_store
    from herald.approvals_service import ApprovalService
    from herald.idle.loop import IdleBudget, IdleLoop

    queue = context.require_queue()
    repo_path = Path(args.repo)
    approvals = ApprovalService(build_approval_store(), queue)
    recipient = args.recipient or os.environ.get("HERALD_RECIPIENT")
    notifier = None
    if recipient:
        from herald.notify.notifier import Notifier
        from herald.worker_factory import build_transport

        notifier = Notifier(build_transport(), approvals)

    def submit(proposal) -> str | None:
        # Deterministic id: the same proposal must not be enqueued twice across ticks.
        digest = hashlib.sha256(
            f"{proposal.repo_path}|{proposal.title}|{proposal.instructions}".encode()
        ).hexdigest()[:32]
        transport_id = f"<idle-{digest}@herald.local>"
        task = Task(
            id=transport_id,
            transport_id=transport_id,
            subject=proposal.title,
            spec=TaskSpec(
                repo_url=args.repo_url or str(proposal.repo_path),
                instructions=proposal.instructions,
            ),
        )
        stored = queue.enqueue(task)
        if stored is None:
            return None
        if not args.no_gate:
            # Proposals are approval-gated by construction: park in Action and issue a
            # single-use token; 'herald approval approve <token>' releases it to run.
            queue.transition(stored, TaskState.ACTION)
            if notifier is not None and recipient:
                notifier.request_approval(
                    stored, recipient=recipient, action="run", summary=proposal.title
                )
            else:
                approvals.request(stored.id, "run")
        return stored.id

    loop = IdleLoop(
        queue=queue,
        submit=submit,
        budget=IdleBudget(max_per_run=args.max, min_queue_depth=args.min_queue_depth),
        repo_name=repo_path.name,
    )
    report = loop.tick(str(repo_path))
    payload = {"skipped": report.skipped, "reason": report.reason, "proposed": report.proposed}
    if context.as_json:
        context.emit_json(payload)
    elif report.skipped:
        context.emit(f"idle skipped: {report.reason}")
    else:
        context.emit(f"proposed {len(report.proposed)}: {', '.join(report.proposed)}")
    return 0


def add_health_commands(
    subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser("health", help="show queue health and per-state counts")
    parser.add_argument(
        "--max-running",
        type=int,
        default=int(os.environ.get("HERALD_MAX_RUNNING", "1")),
        help="running tasks above this mark the queue unhealthy (default: 1)",
    )
    parser.set_defaults(handler=_handle_health)


def _handle_health(args: argparse.Namespace, context: CliContext) -> int:
    from herald.observability.health import Health

    status = Health(max_running=args.max_running).check(context.require_queue())
    if context.as_json:
        context.emit_json(status.to_dict())
    else:
        context.emit(f"ok: {status.ok}")
        for state, count in status.counts.items():
            context.emit(f"{state}: {count}")
    return 0


def _add_parse_reply(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("parse-reply", help="parse a reply body for a decision")
    parser.add_argument("text")
    parser.set_defaults(handler=_handle_parse_reply, needs_queue=False)


def _handle_parse_reply(args: argparse.Namespace, context: CliContext) -> int:
    from herald.approvals_service import parse_reply

    reply = parse_reply(args.text)
    if reply is None:
        context.emit("no approval decision found")
        return 1
    if context.as_json:
        context.emit_json({"token": reply.token, "decision": reply.decision})
    else:
        context.emit(f"{reply.decision} {reply.token}")
    return 0


def add_run_commands(
    subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser("run", help="execute one claimed task end to end")
    parser.add_argument("task_id", nargs="?", default=None, help="task id (default: oldest queued)")
    parser.add_argument("--repo", required=True, help="path to the source repository")
    parser.add_argument("--recipient", default=None, help="email to notify on completion")
    parser.add_argument(
        "--model",
        default=None,
        help="model id passed to the harness (provider:model); default from env",
    )
    parser.add_argument(
        "--backend",
        choices=["memory", "jmap"],
        default="memory",
        help="transport backend (default: memory); the queue comes from HERALD_QUEUE",
    )
    parser.set_defaults(handler=_handle_run)


def _handle_run(args: argparse.Namespace, context: CliContext) -> int:
    from herald.worker_factory import build_transport, build_worker

    queue = context.require_queue()
    # A model hint from the task (e.g. a 'model:' line) applies when --model is not given.
    model = args.model
    if model is None and args.task_id:
        existing = queue.get(args.task_id)
        if existing is not None and existing.spec is not None:
            model = existing.spec.model_request
    transport = build_transport(args.backend)
    worker = build_worker(
        queue=queue,
        transport=transport,
        repo_path=args.repo,
        recipient=args.recipient or None,
        provider_model=model or None,
    )
    # Without an id, claim the oldest queued task atomically (a local sweep tick).
    evidence = worker.run(args.task_id) if args.task_id else worker.run_next()
    payload = {
        "task_id": evidence.task_id,
        "branch": evidence.branch,
        "commit": evidence.commit,
        "pr_url": evidence.pr_url,
    }
    if context.as_json:
        context.emit_json(payload)
    else:
        context.emit(f"{evidence.task_id} {evidence.branch} {evidence.pr_url}")
    # Mirror the task outcome in the exit code so a runner Job reflects success/failure.
    return 0 if evidence.ok else 1


def add_approval_commands(
    subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser
) -> None:
    approval = subparsers.add_parser("approval", help="inspect approval replies")
    approval.set_defaults(handler=None)
    approval_sub = approval.add_subparsers(dest="approval_command", required=True)
    _add_parse_reply(approval_sub)
    _add_pending_approvals(approval_sub)
    _add_redeem(approval_sub, "approve", "approved", "redeem a token as an approval")
    _add_redeem(approval_sub, "reject", "rejected", "redeem a token as a rejection")


def _add_redeem(
    subparsers: argparse._SubParsersAction, name: str, decision: str, help_text: str
) -> None:
    parser = subparsers.add_parser(name, help=help_text)
    parser.add_argument("token")
    parser.set_defaults(handler=_handle_redeem, decision=decision)


def _handle_redeem(args: argparse.Namespace, context: CliContext) -> int:
    from herald.approvals import ApprovalError, build_approval_store
    from herald.approvals_service import ApprovalReply, ApprovalService

    service = ApprovalService(build_approval_store(), context.require_queue())
    try:
        task = service.apply(ApprovalReply(token=args.token, decision=args.decision))
    except ApprovalError as exc:
        raise RuntimeError(str(exc)) from exc
    context.emit(f"{task.id} -> {task.state.value}")
    return 0


def _add_pending_approvals(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("pending", help="list pending approval requests")
    parser.set_defaults(handler=_handle_pending_approvals, needs_queue=False)


def _handle_pending_approvals(args: argparse.Namespace, context: CliContext) -> int:
    from herald.approvals import build_approval_store

    pending = build_approval_store().pending()
    if context.as_json:
        context.emit_json(
            [
                {
                    "task_id": approval.task_id,
                    "action": approval.action,
                    "token": approval.token,
                    "created_at": approval.created_at,
                    "expires_at": approval.expires_at,
                }
                for approval in pending
            ]
        )
    elif not pending:
        context.emit("no pending approvals")
    else:
        for approval in pending:
            # The token is shown so an operator can approve locally; it is single-use and
            # short-lived, and is the same secret the email request carries.
            context.emit(
                f"{approval.task_id} {approval.action} {approval.token} "
                f"expires {approval.expires_at.isoformat()}"
            )
    return 0


def add_decide_commands(
    subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "decide", help="ask the decision model to route a task (ADR 0004)"
    )
    parser.add_argument("state", help="the task text to decide on")
    parser.add_argument(
        "--decider",
        default=None,
        help="override HERALD_DECIDER (rules|jev|openai-compat|none) for this call",
    )
    parser.set_defaults(handler=_handle_decide, needs_queue=False)


def _handle_decide(args: argparse.Namespace, context: CliContext) -> int:
    import os

    from herald.decision.factory import build_decider
    from herald.decision.port import DecisionError
    from herald.decision.router import HUMAN_QUESTION, ROUTE_QUESTION

    if args.decider:
        os.environ["HERALD_DECIDER"] = args.decider
    try:
        decider = build_decider()
    except DecisionError as exc:
        raise RuntimeError(str(exc)) from exc
    if decider is None:
        raise RuntimeError("no decider is configured")

    decision = decider.decide(args.state, {"model": ROUTE_QUESTION, "needs_human": HUMAN_QUESTION})
    payload = {
        "model": decision.choice("model"),
        "model_confidence": decision.confidence("model"),
        "needs_human": decision.noul("needs_human"),
    }
    if context.as_json:
        context.emit_json(payload)
    else:
        context.emit(
            f"model={payload['model']} (confidence={payload['model_confidence']}) "
            f"needs_human={payload['needs_human']}"
        )
    return 0


def add_decision_commands(
    subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser("decision", help="inspect and tune the decision log")
    parser.add_argument(
        "--log",
        default=os.environ.get("HERALD_DECIDER_LOG"),
        help="path to the decision log (default: HERALD_DECIDER_LOG)",
    )
    parser.set_defaults(handler=None)
    sub = parser.add_subparsers(dest="decision_command", required=True)
    _add_calibrate(sub, parser)
    _add_label(sub, parser)


def _add_calibrate(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "calibrate", help="accuracy by confidence band and a recommended threshold"
    )
    parser.add_argument("--question", default="model")
    parser.add_argument("--target", type=float, default=0.95)
    parser.set_defaults(handler=_handle_calibrate, needs_queue=False)


def _log_path(args: argparse.Namespace) -> str:
    if not args.log:
        raise RuntimeError("a decision log is required (--log or HERALD_DECIDER_LOG)")
    return args.log


def _decision_store(args: argparse.Namespace):
    """A file ``DecisionLog`` or, for ``--log postgres``, the database store (ADR 0005)."""
    target = _log_path(args)
    if target == "postgres":
        import os

        from herald.db import connect, init_schema
        from herald.decision.log import PostgresDecisionLog

        dsn = os.environ.get("HERALD_DATABASE_URL")
        if not dsn:
            raise RuntimeError("HERALD_DATABASE_URL is required for --log postgres")
        conn = connect(dsn)
        init_schema(conn)
        return PostgresDecisionLog(conn)
    from pathlib import Path

    from herald.decision.log import DecisionLog

    return DecisionLog(path=Path(target))


def _handle_calibrate(args: argparse.Namespace, context: CliContext) -> int:
    report = _decision_store(args).calibrate(question_id=args.question, target=args.target)
    payload = {
        "question": report.question_id,
        "total": report.total,
        "labeled": report.labeled,
        "accuracy": report.accuracy,
        "recommended_threshold": report.recommended_threshold,
        "bands": [
            {"band": b.label, "count": b.count, "accuracy": b.accuracy} for b in report.bands
        ],
    }
    if context.as_json:
        context.emit_json(payload)
    else:
        context.emit(
            f"{report.question_id}: {report.labeled}/{report.total} labeled, "
            f"accuracy={report.accuracy}"
        )
        for band in report.bands:
            context.emit(f"  {band.label}: n={band.count} accuracy={band.accuracy}")
        context.emit(f"recommended threshold: {report.recommended_threshold}")
    return 0


def _add_label(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser("label", help="attach ground truth to a logged decision")
    parser.add_argument("index", type=int, help="0-based record index")
    parser.add_argument("label", help="the correct answer (e.g. local|hosted)")
    parser.set_defaults(handler=_handle_label, needs_queue=False)


def _handle_label(args: argparse.Namespace, context: CliContext) -> int:
    updated = _decision_store(args).label(args.index, args.label)
    if not updated:
        raise RuntimeError(f"no decision at index {args.index}")
    context.emit(f"labeled decision {args.index} as {args.label}")
    return 0

from __future__ import annotations

import argparse
from datetime import timedelta
from typing import Any

from herald.cli.context import CliContext
from herald.queue.models import Task, TaskState

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
    _print_tasks(context, context.queue.list(TaskState(args.state), limit=args.limit))
    return 0


def _add_show(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("show", help="show one task")
    parser.add_argument("task_id")
    parser.set_defaults(handler=_handle_show)


def _handle_show(args: argparse.Namespace, context: CliContext) -> int:
    task = context.queue.get(args.task_id)
    if task is None:
        context.emit(f"no task {args.task_id!r}")
        return 1
    if context.as_json:
        context.emit_json(_task_payload(task))
    else:
        for key, value in _task_payload(task).items():
            context.emit(f"{key}: {value}")
    return 0


def _add_claim(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("claim", help="claim a queued task (queued -> running)")
    parser.add_argument("task_id")
    parser.add_argument("--lease-seconds", type=int, default=LEASE_SECONDS_DEFAULT)
    parser.set_defaults(handler=_handle_claim)


def _handle_claim(args: argparse.Namespace, context: CliContext) -> int:
    task = context.queue.get(args.task_id)
    if task is None:
        context.emit(f"no task {args.task_id!r}")
        return 1
    if not context.queue.claim(task, lease=timedelta(seconds=args.lease_seconds)):
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
    task = context.queue.get(args.task_id)
    if task is None:
        context.emit(f"no task {args.task_id!r}")
        return 1
    transitioned = context.queue.transition(task, args.target_state)
    context.emit(f"{transitioned.id} -> {transitioned.state.value}")
    return 0


def add_task_commands(
    subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser
) -> None:
    task = subparsers.add_parser("task", help="manage tasks")
    task.add_argument(
        "--backend",
        choices=["memory", "jmap"],
        default="memory",
        help="queue backend (default: memory)",
    )
    task.set_defaults(handler=None)
    task_sub = task.add_subparsers(dest="task_command", required=True)

    _add_list(task_sub, task)
    _add_show(task_sub)
    _add_claim(task_sub)
    _add_terminal(task_sub, "complete", TaskState.DONE, "mark a running task done")
    _add_terminal(task_sub, "fail", TaskState.FAILED, "mark a running task failed")

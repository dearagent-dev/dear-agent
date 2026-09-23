from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from dear_agent.cli.commands import (
    add_approval_commands,
    add_decide_commands,
    add_decision_commands,
    add_health_commands,
    add_idle_commands,
    add_listen_commands,
    add_policy_commands,
    add_poll_commands,
    add_run_commands,
    add_sweep_commands,
    add_task_commands,
)
from dear_agent.cli.context import CliContext, build_queue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dear-agent", description="Dear Agent control plane CLI")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of human-readable text",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_task_commands(subparsers, parser)
    add_run_commands(subparsers, parser)
    add_sweep_commands(subparsers, parser)
    add_listen_commands(subparsers, parser)
    add_poll_commands(subparsers, parser)
    add_policy_commands(subparsers, parser)
    add_decide_commands(subparsers, parser)
    add_decision_commands(subparsers, parser)
    add_approval_commands(subparsers, parser)
    add_idle_commands(subparsers, parser)
    add_health_commands(subparsers, parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handler = getattr(args, "handler", None)
    if handler is None:
        parser.error("no command given")
        return 2

    try:
        queue = build_queue() if getattr(args, "needs_queue", True) else None
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    context = CliContext(queue=queue, as_json=args.json, out=sys.stdout)
    try:
        return handler(args, context)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

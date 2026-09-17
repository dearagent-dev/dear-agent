from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from herald.cli.commands import add_task_commands
from herald.cli.context import CliContext, build_queue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herald", description="Herald control plane CLI")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of human-readable text",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_task_commands(subparsers, parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        queue = build_queue(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    context = CliContext(queue=queue, as_json=args.json, out=sys.stdout)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.error("no command given")
        return 2
    try:
        return handler(args, context)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

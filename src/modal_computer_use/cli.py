from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .tracing import ComputerTrace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="computer-use")
    subparsers = parser.add_subparsers(dest="command", required=True)

    trace_parser = subparsers.add_parser("trace")
    trace_subparsers = trace_parser.add_subparsers(dest="trace_command", required=True)

    validate_parser = trace_subparsers.add_parser("validate")
    validate_parser.add_argument("path", type=Path)

    replay_parser = trace_subparsers.add_parser("replay")
    replay_parser.add_argument("path", type=Path)
    replay_parser.add_argument("--dry-run", action="store_true", required=True)

    args = parser.parse_args(argv)
    if args.command == "trace" and args.trace_command == "validate":
        return _trace_validate(args.path)
    return _trace_replay(args.path)


def _trace_validate(path: Path) -> int:
    result = ComputerTrace.load(path).validate()
    _print_json(result.to_dict())
    return 0 if result.ok else 1


def _trace_replay(path: Path) -> int:
    plan = ComputerTrace.load(path).replay(dry_run=True)
    _print_json(plan.to_dict())
    return 0 if plan.ok else 1


def _print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())

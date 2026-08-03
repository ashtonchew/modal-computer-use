"""Attach to an existing Modal Sandbox without taking ownership of its lifetime."""

from __future__ import annotations

import argparse
import math
import os
from collections.abc import Sequence

from modal_computer_use import ComputerSandbox


def _environment_value(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sandbox-id",
        default=_environment_value("MODAL_COMPUTER_USE_SANDBOX_ID"),
        help="existing Sandbox ID (or set MODAL_COMPUTER_USE_SANDBOX_ID)",
    )
    parser.add_argument(
        "--name",
        default=_environment_value("MODAL_COMPUTER_USE_SANDBOX_NAME"),
        help="existing Sandbox name (or set MODAL_COMPUTER_USE_SANDBOX_NAME)",
    )
    parser.add_argument(
        "--run-id",
        default=_environment_value("MODAL_COMPUTER_USE_RUN_ID"),
        help="existing computer-use run ID (or set MODAL_COMPUTER_USE_RUN_ID)",
    )
    parser.add_argument(
        "--app-name",
        default=_environment_value("MODAL_COMPUTER_USE_APP_NAME") or "modal-computer-use",
        help="owning Modal App name (default: modal-computer-use)",
    )
    parser.add_argument(
        "--modal-environment",
        default=_environment_value("MODAL_ENVIRONMENT"),
        help="Modal Environment containing the existing Sandbox",
    )
    parser.add_argument("--readiness-timeout", type=_positive_float, default=120.0)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    selectors = {
        "sandbox_id": args.sandbox_id,
        "name": args.name,
        "run_id": args.run_id,
    }
    selected = {key: value for key, value in selectors.items() if value}
    if len(selected) != 1:
        parser.error("provide exactly one of --sandbox-id, --name, or --run-id")

    computer = ComputerSandbox.attach(
        **selected,
        app_name=args.app_name,
        modal_environment=args.modal_environment,
        wait=True,
        readiness_timeout=args.readiness_timeout,
    )
    try:
        status = computer.status()
        print({"ready": status.ready})
    finally:
        # This process attached to a caller-owned Sandbox; it must not terminate it.
        computer.detach()


if __name__ == "__main__":
    main()

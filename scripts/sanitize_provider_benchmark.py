from __future__ import annotations

import argparse
from pathlib import Path

from modal_computer_use.benchmarks.artifacts import generate_sanitized_provider_benchmark


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a secret-safe provider benchmark artifact"
    )
    parser.add_argument("raw", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--raw-artifact-path", required=True)
    parser.add_argument("--harness-commit", required=True)
    parser.add_argument(
        "--status",
        required=True,
        choices=["candidate", "current_reference", "historical", "rejected", "superseded"],
    )
    parser.add_argument("--scope", required=True)
    parser.add_argument("--status-reason")
    parser.add_argument("--harness-diff-sha256")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    matches = generate_sanitized_provider_benchmark(
        raw_path=args.raw,
        output_path=args.output,
        raw_artifact_path=args.raw_artifact_path,
        harness_commit=args.harness_commit,
        status=args.status,
        scope=args.scope,
        status_reason=args.status_reason,
        harness_diff_sha256=args.harness_diff_sha256,
        check=args.check,
    )
    if args.check and not matches:
        parser.error("tracked sanitized artifact differs from generated output")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from modal_computer_use.benchmarks.modal_optimization import (
    generate_sanitized_modal_optimization_benchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a secret-safe Modal optimization benchmark artifact"
    )
    parser.add_argument("raw", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--raw-artifact-path", required=True)
    parser.add_argument("--harness-commit", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if not args.check:
        _require_clean_exact_head(args.harness_commit)
    matches = generate_sanitized_modal_optimization_benchmark(
        raw_path=args.raw,
        output_path=args.output,
        raw_artifact_path=args.raw_artifact_path,
        harness_commit=args.harness_commit,
        check=args.check,
    )
    if args.check and not matches:
        parser.error("tracked sanitized artifact differs from generated output")
    return 0


def _require_clean_exact_head(harness_commit: str) -> None:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to publish a benchmark artifact")
    if _git_output(git, "rev-parse", "HEAD") != harness_commit:
        raise RuntimeError("harness commit does not match HEAD")
    if _git_output(git, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("artifact publication requires a clean tracked worktree")


def _git_output(git: str, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        [git, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())

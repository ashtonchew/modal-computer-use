from __future__ import annotations

import argparse
import shutil
import subprocess
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
    harness_state = _verified_harness_state(args.status, args.harness_commit)
    matches = generate_sanitized_provider_benchmark(
        raw_path=args.raw,
        output_path=args.output,
        raw_artifact_path=args.raw_artifact_path,
        harness_commit=args.harness_commit,
        harness_state=harness_state,
        status=args.status,
        scope=args.scope,
        status_reason=args.status_reason,
        harness_diff_sha256=args.harness_diff_sha256,
        check=args.check,
    )
    if args.check and not matches:
        parser.error("tracked sanitized artifact differs from generated output")
    return 0


def _verified_harness_state(status: str, harness_commit: str) -> str:
    if status != "current_reference":
        return "dirty" if status == "candidate" else "clean"
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to publish a current_reference artifact")
    head = _git_output(git, "rev-parse", "HEAD")
    if head != harness_commit:
        raise RuntimeError("current_reference harness commit does not match HEAD")
    if _git_output(git, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("current_reference requires a clean tracked worktree")
    return "clean"


def _git_output(git: str, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        [git, *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())

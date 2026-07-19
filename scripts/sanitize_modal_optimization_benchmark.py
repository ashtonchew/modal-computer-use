from __future__ import annotations

import argparse
import json
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
    parser.add_argument(
        "--preregistration",
        type=Path,
        help="preregistration manifest; defaults to preregistration.json beside raw",
    )
    parser.add_argument(
        "--region-evidence",
        type=Path,
        help="source-bound region evidence; defaults beside raw",
    )
    parser.add_argument("--v2-raw", type=Path)
    parser.add_argument("--v2-raw-artifact-path")
    parser.add_argument("--v2-preregistration", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    normalizer_commit = _normalizer_commit(
        args.harness_commit,
        output_path=args.output,
        check=args.check,
    )
    matches = generate_sanitized_modal_optimization_benchmark(
        raw_path=args.raw,
        output_path=args.output,
        raw_artifact_path=args.raw_artifact_path,
        harness_commit=args.harness_commit,
        preregistration_path=(
            args.preregistration or args.raw.parent / "preregistration.json"
        ),
        region_evidence_path=(
            args.region_evidence
            or args.raw.parent / "region-selection-attested.json"
        ),
        normalizer_commit=normalizer_commit,
        v2_raw_path=args.v2_raw,
        v2_raw_artifact_path=args.v2_raw_artifact_path,
        v2_preregistration_path=args.v2_preregistration,
        check=args.check,
    )
    if args.check and not matches:
        parser.error("tracked sanitized artifact differs from generated output")
    return 0


def _normalizer_commit(
    harness_commit: str,
    *,
    output_path: Path,
    check: bool,
) -> str:
    if check:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        return str(payload["provenance"]["normalizer_sha"])
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to publish a benchmark artifact")
    head = _git_output(git, "rev-parse", "HEAD")
    ancestor = subprocess.run(  # noqa: S603
        [git, "merge-base", "--is-ancestor", harness_commit, head],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if ancestor.returncode != 0:
        raise RuntimeError("execution harness must be an ancestor of the normalizer")
    if _git_output(git, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("artifact publication requires a clean tracked worktree")
    return head


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

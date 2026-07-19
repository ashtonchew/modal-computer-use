from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from modal_computer_use.benchmarks.modal_v2_placement import (
    DEFAULT_CLOUD_REQUESTS,
    placement_capability_sha256,
    run_placement_capability_matrix,
    serialize_placement_capability,
    validate_placement_artifact_path,
)

DEFAULT_OUTPUT = Path(
    "benchmark-results/modal-v2-candidate-2026-07-19/diagnostics/placement-capability.json"
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe unmeasured Modal V1/V2 placement compatibility"
    )
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--image-revision")
    parser.add_argument("--region", default="us-west")
    parser.add_argument(
        "--cloud-candidate",
        action="append",
        choices=("unconstrained", "aws", "gcp", "oci"),
        help="Repeat to override the default ordered capability matrix",
    )
    parser.add_argument("--cpu", type=float, default=4.0)
    parser.add_argument("--memory-mib", type=int, default=8192)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    _require_clean_source(args.source_sha)
    _require_benchmark_results_path(args.output)
    cloud_requests = (
        DEFAULT_CLOUD_REQUESTS
        if args.cloud_candidate is None
        else tuple(None if value == "unconstrained" else value for value in args.cloud_candidate)
    )
    payload = run_placement_capability_matrix(
        run_id=f"modal-v2-placement-{uuid.uuid4().hex}",
        source_sha=args.source_sha,
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        image_revision=args.image_revision or args.source_sha,
        region=args.region,
        target_cpu=args.cpu,
        target_memory_mib=args.memory_mib,
        cloud_requests=cloud_requests,
    )
    if args.output.exists():
        raise RuntimeError(f"output already exists: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(f"{args.output.suffix}.tmp")
    temporary.write_bytes(serialize_placement_capability(payload))
    temporary.replace(args.output)
    print(
        json.dumps(
            {
                "status": (
                    "eligible"
                    if payload["backend_causal_comparison_available"] is True
                    else "descriptive-only"
                ),
                "output": str(args.output),
                "sha256": placement_capability_sha256(payload),
                "selected_request": payload["selected_request"],
            }
        )
    )
    return 0 if payload["backend_causal_comparison_available"] is True else 2


def _require_clean_source(source_sha: str) -> None:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required for placement-probe provenance")
    if _git_output(git, "rev-parse", "HEAD") != source_sha:
        raise RuntimeError("source SHA does not match HEAD")
    if _git_output(git, "status", "--porcelain"):
        raise RuntimeError("placement probes require a clean worktree")


def _git_output(git: str, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        [git, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _require_benchmark_results_path(path: Path) -> None:
    try:
        validate_placement_artifact_path(path.as_posix())
    except ValueError as exc:
        raise ValueError("placement-probe output must be under benchmark-results") from exc


if __name__ == "__main__":
    raise SystemExit(main())

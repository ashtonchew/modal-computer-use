from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from modal_computer_use.benchmarks.image_lifecycle import (
    ImageLifecycleBenchmarkSpec,
    validate_image_lifecycle_artifact,
)
from modal_computer_use.benchmarks.modal_image_lifecycle import (
    run_modal_image_lifecycle,
)
from modal_computer_use.image import load_image_release_record

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PILOT_SAMPLES_PER_ARM = 2
PRIMARY_SAMPLES_PER_ARM = 30
PILOT_SCHEDULE_SEED = 20260808
PRIMARY_SCHEDULE_SEED = 20260809


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the paired inline-recipe and managed-exact-id Modal Image lifecycle "
            "Benchmark Surface."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("pilot", "primary"):
        subparser = subparsers.add_parser(command)
        _common_arguments(subparser)
        if command == "primary":
            subparser.add_argument("--pilot-result", required=True, type=Path)
    args = parser.parse_args()

    head, clean = _git_state()
    if not clean:
        parser.error("Image lifecycle evidence requires a clean worktree")
    if args.source_sha != head:
        parser.error("--source-sha must match the current HEAD")

    output = _validated_output_path(args.output)
    if output.exists():
        parser.error("--output must not already exist")
    release_record = load_image_release_record(args.manifest)
    run_kind = str(args.command)
    spec = ImageLifecycleBenchmarkSpec(
        source_revision=args.source_sha,
        release_record=release_record,
        run_kind=run_kind,
        samples_per_arm=(
            PILOT_SAMPLES_PER_ARM
            if run_kind == "pilot"
            else PRIMARY_SAMPLES_PER_ARM
        ),
        warmup_pairs=1,
        schedule_seed=(
            PILOT_SCHEDULE_SEED
            if run_kind == "pilot"
            else PRIMARY_SCHEDULE_SEED
        ),
        requested_region=args.region,
        cpu=args.cpu,
        memory_mib=args.memory_mib,
        sandbox_timeout_seconds=args.sandbox_timeout_seconds,
        max_estimated_cost_usd=args.max_estimated_cost_usd,
        benchmark_run_id=f"image-lifecycle-{run_kind}-{args.source_sha[:12]}",
        app_name=args.app_name,
    )
    if run_kind == "primary":
        pilot = _read_artifact(args.pilot_result)
        if pilot.get("status") != "complete":
            raise RuntimeError("primary Image lifecycle run requires a complete pilot")
        validate_image_lifecycle_artifact(pilot)
        _require_pilot_matches_spec(pilot, spec)

    artifact = run_modal_image_lifecycle(spec)
    _write_new(output, artifact)
    print(json.dumps({"status": artifact["status"], "output": str(output)}))
    return 0 if artifact["status"] == "complete" else 2


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--region", required=True)
    parser.add_argument("--cpu", type=float, default=1.0)
    parser.add_argument("--memory-mib", type=int, default=2048)
    parser.add_argument("--sandbox-timeout-seconds", type=int, default=180)
    parser.add_argument("--max-estimated-cost-usd", type=float, required=True)
    parser.add_argument(
        "--app-name",
        default="modal-computer-use-image-lifecycle",
    )
    parser.add_argument("--output", required=True, type=Path)


def _require_pilot_matches_spec(
    pilot: dict[str, Any], spec: ImageLifecycleBenchmarkSpec
) -> None:
    configuration = pilot.get("configuration")
    if not isinstance(configuration, dict):
        raise RuntimeError("pilot Image lifecycle configuration is missing")
    release = configuration.get("managed_release")
    resources = configuration.get("resources")
    if not isinstance(release, dict) or not isinstance(resources, dict):
        raise RuntimeError("pilot Image lifecycle configuration is incomplete")
    expected = {
        "source_revision": spec.source_revision,
        "app_name": spec.app_name,
        "requested_region": spec.requested_region,
        "resources": {"cpu": spec.cpu, "memory_mib": spec.memory_mib},
        "resource_limits": {"cpu": spec.cpu, "memory_mib": spec.memory_mib},
        "sandbox_timeout_seconds": spec.sandbox_timeout_seconds,
        "max_estimated_cost_usd": spec.max_estimated_cost_usd,
        "modal_image_object_id": spec.release_record.modal_image_object_id,
        "image_reference": spec.release_record.image_reference,
        "image_variant": spec.release_record.image_variant,
        "workspace_name": spec.release_record.workspace_name,
        "environment_name": spec.release_record.environment_name,
        "pyproject_sha256": spec.release_record.pyproject_sha256,
        "uv_lock_sha256": spec.release_record.uv_lock_sha256,
        "image_builder_version": spec.release_record.image_builder_version,
        "uv_version": spec.release_record.uv_version,
        "modal_sdk_version": spec.release_record.modal_sdk_version,
    }
    observed = {
        "source_revision": configuration.get("source_revision"),
        "app_name": configuration.get("app_name"),
        "requested_region": configuration.get("requested_region"),
        "resources": resources,
        "resource_limits": configuration.get("resource_limits"),
        "sandbox_timeout_seconds": configuration.get("sandbox_timeout_seconds"),
        "max_estimated_cost_usd": configuration.get("max_estimated_cost_usd"),
        "modal_image_object_id": release.get("modal_image_object_id"),
        "image_reference": release.get("image_reference"),
        "image_variant": release.get("image_variant"),
        "workspace_name": release.get("workspace_name"),
        "environment_name": release.get("environment_name"),
        "pyproject_sha256": release.get("pyproject_sha256"),
        "uv_lock_sha256": release.get("uv_lock_sha256"),
        "image_builder_version": release.get("image_builder_version"),
        "uv_version": release.get("uv_version"),
        "modal_sdk_version": release.get("modal_sdk_version"),
    }
    if observed != expected:
        raise RuntimeError("primary Image lifecycle inputs differ from the pilot")


def _validated_output_path(path: Path) -> Path:
    benchmark_root = (REPOSITORY_ROOT / "benchmark-results").resolve()
    candidate = (path if path.is_absolute() else REPOSITORY_ROOT / path).resolve()
    try:
        candidate.relative_to(benchmark_root)
    except ValueError as exc:
        raise ValueError("Image lifecycle output must be under benchmark-results") from exc
    current = candidate.parent
    while current != benchmark_root.parent:
        if current.is_symlink():
            raise ValueError("Image lifecycle output must not traverse a symlink")
        if current == benchmark_root:
            break
        current = current.parent
    return candidate


def _read_artifact(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("could not read Image lifecycle evidence") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Image lifecycle evidence must be an object")
    return payload


def _write_new(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _git_state() -> tuple[str, bool]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to run Image lifecycle evidence")
    revision = subprocess.run(  # noqa: S603 - resolved binary and fixed arguments.
        [git, "-C", str(REPOSITORY_ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    status = subprocess.run(  # noqa: S603 - resolved binary and fixed arguments.
        [git, "-C", str(REPOSITORY_ROOT), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    return revision, not status.strip()


if __name__ == "__main__":
    raise SystemExit(main())

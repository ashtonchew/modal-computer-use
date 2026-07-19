from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from modal_computer_use.benchmarks.artifacts import validate_sanitized_provider_benchmark
from modal_computer_use.benchmarks.modal_optimization import (
    PROFILE_MODAL_V2,
    ModalOptimizationConfig,
    build_modal_optimization_artifact,
    build_modal_region_evidence_envelope,
    build_modal_v2_profile_artifact,
    build_preregistration,
    build_v2_preregistration,
    select_modal_optimization_region,
    validate_preregistered_config,
)
from modal_computer_use.benchmarks.modal_optimization_execution import (
    run_independent_cold_attempts,
    run_warm_action_attempts,
    run_warm_claim_attempts,
)
from modal_computer_use.sandbox import create_modal_v2_tunnel_computer

DEPENDENCY_SHA = "37f977f80de93800c005caeec7ead5222b00b040"
BASE_BENCHMARK_SOURCE_SHA = "8c21cf1338fd747dca57bca6941c307270069712"
DEFAULT_RAW_ROOT = Path("benchmark-results/modal-optimization-2026-07-19")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the preregistered post-optimization Modal benchmark"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preregister = subparsers.add_parser("preregister")
    _add_common_arguments(preregister, region_default="selection-pending")
    preregister.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_RAW_ROOT / "preregistration.json",
    )

    region = subparsers.add_parser("region")
    region.add_argument("--source-sha", required=True)
    region.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_RAW_ROOT / "region-selection.json",
    )

    attest_region = subparsers.add_parser("attest-region")
    attest_region.add_argument("raw", type=Path)
    attest_region.add_argument("output", type=Path)
    attest_region.add_argument("--source-sha", required=True)
    attest_region.add_argument("--raw-artifact-path", required=True)

    run = subparsers.add_parser("run")
    _add_common_arguments(run, region_default="selection-pending")
    run.add_argument("--provider-default", type=Path, required=True)
    run.add_argument("--region-selection", type=Path, required=True)
    run.add_argument(
        "--preregistration",
        type=Path,
        default=DEFAULT_RAW_ROOT / "preregistration.json",
    )
    run.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_RAW_ROOT / "raw.json",
    )

    preregister_v2 = subparsers.add_parser("preregister-v2")
    _add_v2_arguments(preregister_v2)
    preregister_v2.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_RAW_ROOT / "v2-preregistration.json",
    )

    run_v2 = subparsers.add_parser("run-v2")
    _add_v2_arguments(run_v2)
    run_v2.add_argument(
        "--preregistration",
        type=Path,
        default=DEFAULT_RAW_ROOT / "v2-preregistration.json",
    )
    run_v2.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_RAW_ROOT / "v2-raw.json",
    )
    args = parser.parse_args()
    if args.command == "preregister":
        return _preregister(args)
    if args.command == "region":
        return _run_region(args)
    if args.command == "attest-region":
        return _attest_region(args)
    if args.command == "preregister-v2":
        return _preregister_v2(args)
    if args.command == "run-v2":
        return _run_v2(args)
    return _run(args)


def _add_common_arguments(parser: argparse.ArgumentParser, *, region_default: str | None) -> None:
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--dependency-sha", default=DEPENDENCY_SHA)
    parser.add_argument("--region-selection-source-sha")
    parser.add_argument("--provider-default-source-sha")
    parser.add_argument("--region", required=region_default is None, default=region_default)
    parser.add_argument("--image-revision")
    parser.add_argument("--cold-attempts", type=int, default=30)
    parser.add_argument("--warm-action-attempts", type=int, default=30)
    parser.add_argument("--warm-claim-attempts", type=int, default=30)
    parser.add_argument("--warm-pool-target", type=int, default=3)
    parser.add_argument("--warm-idle-seconds", type=float, default=30.0)


def _add_v2_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--dependency-sha", default=DEPENDENCY_SHA)
    parser.add_argument("--base-benchmark-source-sha", default=BASE_BENCHMARK_SOURCE_SHA)
    parser.add_argument("--region", default="us-west")
    parser.add_argument("--image-revision")
    parser.add_argument("--cold-attempts", type=int, default=30)
    parser.add_argument("--warm-action-attempts", type=int, default=30)


def _config(args: argparse.Namespace) -> ModalOptimizationConfig:
    return ModalOptimizationConfig(
        region=args.region,
        image_revision=args.image_revision or args.source_sha,
        cold_attempts=args.cold_attempts,
        warm_action_attempts=args.warm_action_attempts,
        warm_claim_attempts=args.warm_claim_attempts,
        warm_pool_target=args.warm_pool_target,
        warm_idle_seconds=args.warm_idle_seconds,
    )


def _v2_config(args: argparse.Namespace) -> ModalOptimizationConfig:
    return ModalOptimizationConfig(
        region=args.region,
        image_revision=args.image_revision or args.source_sha,
        cold_attempts=args.cold_attempts,
        warm_action_attempts=args.warm_action_attempts,
        warm_claim_attempts=1,
        warm_pool_target=1,
        warm_idle_seconds=0.0,
        ingress="tunnel",
    )


def _preregister(args: argparse.Namespace) -> int:
    _require_dependency(args.source_sha, args.dependency_sha, require_clean=True)
    config = _config(args)
    region_selection_source_sha = args.region_selection_source_sha or args.source_sha
    provider_default_source_sha = args.provider_default_source_sha or args.source_sha
    commands = _commands(
        args.source_sha,
        region_selection_source_sha=region_selection_source_sha,
        provider_default_source_sha=provider_default_source_sha,
    )
    payload = build_preregistration(
        config,
        source_sha=args.source_sha,
        dependency_sha=args.dependency_sha,
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        runner_identity={
            "kind": "local",
            "operating_system": platform.system(),
            "architecture": platform.machine(),
            "timezone": "America/Los_Angeles",
            "location_label": "local-macos-arm64-America-Los_Angeles",
        },
        sdk_versions={
            "modal": version("modal"),
            "daytona": version("daytona"),
            "e2b-desktop": version("e2b-desktop"),
        },
        commands=commands,
    )
    payload["region_selection_evidence_source_sha"] = region_selection_source_sha
    payload["provider_default_evidence_source_sha"] = provider_default_source_sha
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    print(json.dumps({"status": "preregistered", "output": str(args.output)}))
    return 0


def _run_region(args: argparse.Namespace) -> int:
    _require_dependency(args.source_sha, DEPENDENCY_SHA, require_clean=True)
    if args.output.exists():
        raise RuntimeError("region output already exists; refusing stale evidence")
    executable = shutil.which("computer-use")
    if executable is None:
        raise RuntimeError("computer-use is required for region evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=args.output.parent,
        prefix=".region-selection-",
        suffix=".json",
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        subprocess.run(  # noqa: S603
            [
                executable,
                "benchmark",
                "modal-region-ab",
                "--modal-region",
                "default",
                "--modal-region",
                "us-west",
                "--modal-region",
                "us-east",
                "--modal-ingress",
                "attested-tunnel",
                "--caller-region-label",
                "local-macos-arm64-America-Los_Angeles",
                "--resource-profile",
                "browser",
                "--browser",
                "chromium",
                "--modal-cpu",
                "4",
                "--modal-memory-mib",
                "8192",
                "--iterations",
                "30",
                "--output",
                str(temporary),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        payload = json.loads(temporary.read_bytes())
        if not isinstance(payload, dict) or payload.get("benchmark") != "modal-region-ab":
            raise ValueError("region runner did not produce modal-region-ab evidence")
        payload["execution_source_sha"] = args.source_sha
        args.output.write_text(
            f"{json.dumps(payload, indent=2, sort_keys=True)}\n",
            encoding="utf-8",
        )
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({"status": "complete", "output": str(args.output)}))
    return 0


def _preregister_v2(args: argparse.Namespace) -> int:
    _require_dependency(args.source_sha, args.dependency_sha, require_clean=True)
    _require_dependency(
        args.source_sha,
        args.base_benchmark_source_sha,
        require_clean=True,
    )
    config = _v2_config(args)
    modal_sdk_version = version("modal")
    if modal_sdk_version != "1.5.2":
        raise RuntimeError("Modal V2 benchmark execution requires Modal SDK 1.5.2")
    payload = build_v2_preregistration(
        config,
        source_sha=args.source_sha,
        dependency_sha=args.dependency_sha,
        base_benchmark_source_sha=args.base_benchmark_source_sha,
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        runner_identity={
            "kind": "local",
            "operating_system": platform.system(),
            "architecture": platform.machine(),
            "timezone": "America/Los_Angeles",
            "location_label": "local-macos-arm64-America-Los_Angeles",
        },
        sdk_versions={"modal": modal_sdk_version},
        commands=_v2_commands(
            args.source_sha,
            base_benchmark_source_sha=args.base_benchmark_source_sha,
            config=config,
        ),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    print(json.dumps({"status": "preregistered", "output": str(args.output)}))
    return 0


def _run_v2(args: argparse.Namespace) -> int:
    _require_dependency(args.source_sha, args.dependency_sha, require_clean=True)
    _require_dependency(
        args.source_sha,
        args.base_benchmark_source_sha,
        require_clean=True,
    )
    config = _v2_config(args)
    preregistration_bytes = args.preregistration.read_bytes()
    preregistration = json.loads(preregistration_bytes)
    if preregistration.get("source_sha") != args.source_sha:
        raise RuntimeError("V2 preregistration source differs from the harness")
    if preregistration.get("base_benchmark_source_sha") != args.base_benchmark_source_sha:
        raise RuntimeError("V2 preregistration targets a different base benchmark")
    if preregistration.get("dependency", {}).get("head_sha") != args.dependency_sha:
        raise RuntimeError("V2 preregistration dependency differs from PR #114")
    modal_sdk_version = version("modal")
    preregistered_modal_sdk_version = (
        preregistration.get("environment", {}).get("sdk_versions", {}).get("modal")
    )
    if modal_sdk_version != "1.5.2" or modal_sdk_version != preregistered_modal_sdk_version:
        raise RuntimeError("installed Modal SDK differs from the V2 preregistration")
    frozen = preregistration.get("configuration")
    expected = {
        "region": config.region,
        "image_revision": config.image_revision,
        "browser": config.browser,
        "ingress": config.ingress,
        "cpu": config.cpu,
        "memory_mib": config.memory_mib,
        "sandbox_timeout_seconds": config.sandbox_timeout_seconds,
        "readiness_timeout_seconds": config.readiness_timeout_seconds,
    }
    if frozen != expected:
        raise RuntimeError("V2 effective configuration differs from preregistration")
    _validate_v2_sample_policy(preregistration, config)
    cold_attempts = run_independent_cold_attempts(
        config,
        create_computer=create_modal_v2_tunnel_computer,
        progress=_progress,
        profile=PROFILE_MODAL_V2,
        progress_label="v2_cold",
    )
    warm_attempts, warm_metadata = run_warm_action_attempts(
        config,
        create_computer=create_modal_v2_tunnel_computer,
        progress=_progress,
        profile=PROFILE_MODAL_V2,
        runner_path="inherited",
        progress_label="v2_warm_action",
    )
    payload = build_modal_v2_profile_artifact(
        config,
        source_sha=args.source_sha,
        dependency_sha=args.dependency_sha,
        base_benchmark_source_sha=args.base_benchmark_source_sha,
        modal_sdk_version=modal_sdk_version,
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        preregistration_sha256=hashlib.sha256(preregistration_bytes).hexdigest(),
        cold_attempts=cold_attempts,
        warm_action_attempts=warm_attempts,
        warm_action_metadata=warm_metadata,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    print(json.dumps({"status": "complete", "output": str(args.output)}))
    return 0


def _run(args: argparse.Namespace) -> int:
    _require_dependency(args.source_sha, args.dependency_sha, require_clean=True)
    region_selection_source_sha = args.region_selection_source_sha or args.source_sha
    provider_default_source_sha = args.provider_default_source_sha or args.source_sha
    _require_dependency(
        args.source_sha,
        region_selection_source_sha,
        require_clean=True,
    )
    _require_dependency(
        args.source_sha,
        provider_default_source_sha,
        require_clean=True,
    )
    selected_region, region_selection = _select_region(
        args.region_selection,
        expected_source_sha=region_selection_source_sha,
    )
    if args.region not in {"selection-pending", selected_region}:
        raise RuntimeError("explicit region does not match preregistered selection evidence")
    args.region = selected_region
    config = _config(args)
    preregistration_bytes = args.preregistration.read_bytes()
    preregistration = json.loads(preregistration_bytes)
    if preregistration.get("source_sha") != args.source_sha:
        raise RuntimeError("preregistration source SHA does not match the benchmark harness")
    if preregistration.get("dependency", {}).get("head_sha") != args.dependency_sha:
        raise RuntimeError("preregistration dependency SHA does not match PR #114")
    validate_preregistered_config(config, preregistration)
    if (
        preregistration.get("region_selection_evidence_source_sha")
        != region_selection_source_sha
    ):
        raise RuntimeError("region evidence source SHA differs from preregistration")
    if (
        preregistration.get("provider_default_evidence_source_sha")
        != provider_default_source_sha
    ):
        raise RuntimeError("provider evidence source SHA differs from preregistration")
    provider_default = json.loads(args.provider_default.read_bytes())
    if not isinstance(provider_default, dict):
        raise ValueError("provider-default artifact must be a JSON object")
    validate_sanitized_provider_benchmark(provider_default)
    if provider_default.get("provenance", {}).get("harness_commit") != provider_default_source_sha:
        raise RuntimeError("provider-default source SHA does not match its provenance")

    progress = _progress
    cold_attempts = run_independent_cold_attempts(config, progress=progress)
    warm_attempts, warm_metadata = run_warm_action_attempts(config, progress=progress)
    claim_attempts, claim_metadata = run_warm_claim_attempts(config, progress=progress)
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload = build_modal_optimization_artifact(
        config,
        source_sha=args.source_sha,
        dependency_sha=args.dependency_sha,
        generated_at=generated_at,
        preregistration_sha256=hashlib.sha256(preregistration_bytes).hexdigest(),
        provider_default_payload=provider_default,
        cold_attempts=cold_attempts,
        warm_action_attempts=warm_attempts,
        warm_action_metadata=warm_metadata,
        claim_attempts=claim_attempts,
        claim_metadata=claim_metadata,
        region_selection=region_selection,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    print(json.dumps({"status": "complete", "output": str(args.output)}))
    return 0


def _attest_region(args: argparse.Namespace) -> int:
    _require_dependency(args.source_sha, DEPENDENCY_SHA, require_clean=True)
    raw_bytes = args.raw.read_bytes()
    payload = json.loads(raw_bytes)
    if not isinstance(payload, dict):
        raise ValueError("region evidence input must be a JSON object")
    envelope = build_modal_region_evidence_envelope(
        payload,
        raw_bytes=raw_bytes,
        raw_artifact_path=args.raw_artifact_path,
        execution_source_sha=args.source_sha,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        f"{json.dumps(envelope, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "attested", "output": str(args.output)}))
    return 0


def _commands(
    source_sha: str,
    *,
    region_selection_source_sha: str,
    provider_default_source_sha: str,
) -> dict[str, str]:
    root = "benchmark-results/modal-optimization-2026-07-19"
    return {
        "region_selection": (
            "uv run python scripts/run_modal_optimization_benchmark.py region "
            f"--source-sha {region_selection_source_sha} "
            f"--output {root}/region-selection.json"
        ),
        "region_selection_attest": (
            "uv run python scripts/run_modal_optimization_benchmark.py attest-region "
            f"{root}/region-selection.json {root}/region-selection-attested.json "
            f"--source-sha {region_selection_source_sha} "
            f"--raw-artifact-path {root}/region-selection.json"
        ),
        "provider_default": (
            "uv run computer-use benchmark compare --create-modal-sandbox "
            "--provider modal-daemon --provider daytona --provider e2b "
            "--modal-ingress attested-tunnel --resource-profile browser "
            "--browser chromium --iterations 3 --env-file "
            "/Users/ashtonchew/projects/modal-computer-use/.env "
            f"--output {root}/provider-default-raw.json --json"
        ),
        "provider_default_normalize": (
            "uv run python scripts/sanitize_provider_benchmark.py "
            f"{root}/provider-default-raw.json {root}/provider-default-sanitized.json "
            f"--raw-artifact-path {root}/provider-default-raw.json "
            f"--harness-commit {provider_default_source_sha} --status current_reference "
            "--scope \"provider-default verification for Modal, Daytona, and E2B "
            "without warm pools\""
        ),
        "publish_image": (
            f"uv run python scripts/publish_modal_images.py --revision {source_sha}"
        ),
        "benchmark": (
            "uv run python scripts/run_modal_optimization_benchmark.py run "
            f"--source-sha {source_sha} --dependency-sha {DEPENDENCY_SHA} "
            f"--region-selection-source-sha {region_selection_source_sha} "
            f"--provider-default-source-sha {provider_default_source_sha} "
            f"--region-selection {root}/region-selection-attested.json "
            f"--provider-default {root}/provider-default-sanitized.json "
            f"--preregistration {root}/preregistration.json --output {root}/raw.json"
        ),
        "normalize": (
            "uv run python scripts/sanitize_modal_optimization_benchmark.py "
            f"{root}/raw.json benchmark-data/modal-optimization-results-2026-07-19.json "
            f"--raw-artifact-path {root}/raw.json --harness-commit {source_sha} "
            f"--preregistration {root}/preregistration.json "
            f"--region-evidence {root}/region-selection-attested.json"
        ),
    }


def _v2_commands(
    source_sha: str,
    *,
    base_benchmark_source_sha: str,
    config: ModalOptimizationConfig,
) -> dict[str, str]:
    root = "benchmark-results/modal-optimization-2026-07-19"
    return {
        "publish_image": (
            f"uv run python scripts/publish_modal_images.py --revision {source_sha}"
        ),
        "benchmark_v2": (
            "uv run python scripts/run_modal_optimization_benchmark.py run-v2 "
            f"--source-sha {source_sha} --dependency-sha {DEPENDENCY_SHA} "
            f"--base-benchmark-source-sha {base_benchmark_source_sha} "
            f"--region {config.region} --image-revision {config.image_revision} "
            f"--cold-attempts {config.cold_attempts} "
            f"--warm-action-attempts {config.warm_action_attempts} "
            f"--preregistration {root}/v2-preregistration.json "
            f"--output {root}/v2-raw.json"
        ),
        "normalize": (
            "uv run python scripts/sanitize_modal_optimization_benchmark.py "
            f"{root}/raw.json benchmark-data/modal-optimization-results-2026-07-19.json "
            f"--raw-artifact-path {root}/raw.json "
            f"--harness-commit {base_benchmark_source_sha} "
            f"--preregistration {root}/preregistration.json "
            f"--region-evidence {root}/region-selection-attested.json "
            f"--v2-raw {root}/v2-raw.json "
            f"--v2-raw-artifact-path {root}/v2-raw.json "
            f"--v2-preregistration {root}/v2-preregistration.json"
        ),
    }


def _validate_v2_sample_policy(
    preregistration: dict[str, Any],
    config: ModalOptimizationConfig,
) -> None:
    expected = {
        "independent_cold_attempts": config.cold_attempts,
        "warm_action_attempts": config.warm_action_attempts,
    }
    if preregistration.get("sample_policy") != expected:
        raise RuntimeError("V2 sample counts differ from preregistration")


def _require_dependency(source_sha: str, dependency_sha: str, *, require_clean: bool) -> None:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required for benchmark provenance")
    if _git_output(git, "rev-parse", "HEAD") != source_sha:
        raise RuntimeError("source SHA does not match HEAD")
    result = subprocess.run(  # noqa: S603
        [git, "merge-base", "--is-ancestor", dependency_sha, source_sha],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError("PR #114 dependency is not an ancestor of the benchmark harness")
    if require_clean and _git_output(git, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("credentialed benchmark execution requires a clean tracked worktree")


def _select_region(
    path: Path,
    *,
    expected_source_sha: str,
) -> tuple[str, dict[str, Any]]:
    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes)
    if not isinstance(payload, dict):
        raise ValueError("region selection artifact must be a JSON object")
    return select_modal_optimization_region(
        payload,
        raw_bytes=raw_bytes,
        expected_source_sha=expected_source_sha,
    )


def _git_output(git: str, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        [git, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _progress(profile: str, completed: int, attempted: int) -> None:
    print(
        json.dumps(
            {
                "status": "progress",
                "profile": profile,
                "completed": completed,
                "attempted": attempted,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())

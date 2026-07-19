from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from modal_computer_use.benchmarks.modal_v2_candidate import (
    CANONICAL_ARMS,
    ModalV2CandidateConfig,
    build_preregistration,
    build_result_artifact,
    classified_raw_artifact_path,
    lifecycle_gate_failure_reason,
    preregistration_sha256,
    promotion_gate_failure_reason,
    serialize_json,
    validate_result_artifact,
)
from modal_computer_use.benchmarks.modal_v2_candidate_execution import (
    run_candidate_phase,
    run_candidate_throughput,
)

DEFAULT_ROOT = Path("benchmark-results/modal-v2-candidate-2026-07-19")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the preregistered four-arm Modal V2 candidate benchmark"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preregister = subparsers.add_parser("preregister")
    _add_configuration_arguments(preregister)
    preregister.add_argument("--source-sha", required=True)
    preregister.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_ROOT / "preregistration.json",
    )

    pilot = subparsers.add_parser("pilot")
    _add_execution_arguments(pilot, default_output=DEFAULT_ROOT / "candidates/pilot.json")

    full = subparsers.add_parser("full")
    _add_execution_arguments(full, default_output=DEFAULT_ROOT / "candidates/full.json")
    full.add_argument(
        "--pilot-result",
        type=Path,
        default=DEFAULT_ROOT / "candidates/pilot.json",
    )

    args = parser.parse_args()
    if args.command == "preregister":
        return _preregister(args)
    if args.command == "pilot":
        return _pilot(args)
    return _full(args)


def _add_configuration_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--image-revision")
    parser.add_argument("--cloud", default="aws")
    parser.add_argument("--region", default="us-west")
    parser.add_argument("--cpu", type=float, default=4.0)
    parser.add_argument("--memory-mib", type=int, default=8192)
    parser.add_argument("--order-seed", type=int, default=20260719)
    parser.add_argument("--bootstrap-seed", type=int, default=20260720)
    parser.add_argument("--bootstrap-resamples", type=int, default=2_000)
    parser.add_argument("--max-estimated-cost-usd", type=float, default=10.0)
    parser.add_argument("--enable-concurrency-50", action="store_true")


def _add_execution_arguments(parser: argparse.ArgumentParser, *, default_output: Path) -> None:
    parser.add_argument("--source-sha", required=True)
    parser.add_argument(
        "--preregistration",
        type=Path,
        default=DEFAULT_ROOT / "preregistration.json",
    )
    parser.add_argument("--output", type=Path, default=default_output)


def _preregister(args: argparse.Namespace) -> int:
    _require_clean_source(args.source_sha)
    config = ModalV2CandidateConfig(
        image_revision=args.image_revision or args.source_sha,
        cloud=args.cloud,
        region=args.region,
        cpu=args.cpu,
        memory_mib=args.memory_mib,
        order_seed=args.order_seed,
        bootstrap_seed=args.bootstrap_seed,
        bootstrap_resamples=args.bootstrap_resamples,
        max_estimated_cost_usd=args.max_estimated_cost_usd,
        enable_concurrency_50=args.enable_concurrency_50,
    )
    payload = build_preregistration(
        config,
        source_sha=args.source_sha,
        generated_at=_utc_now(),
        sdk_versions={"modal": version("modal")},
        package_versions={"modal-computer-use": version("modal-computer-use")},
        runner_identity={
            "control_caller": "local",
            "operating_system": platform.system(),
            "architecture": platform.machine(),
            "timezone": "America/Los_Angeles",
            "measured_runner": "persistent-modal-v2-i6pn",
        },
        commands=_commands(args.source_sha),
    )
    _write_new(args.output, payload)
    print(json.dumps({"status": "preregistered", "output": str(args.output)}))
    return 0


def _pilot(args: argparse.Namespace) -> int:
    _require_clean_source(args.source_sha)
    preregistration = _read_preregistration(args.preregistration, source_sha=args.source_sha)
    config = _config_from_preregistration(preregistration)
    trials, execution = run_candidate_phase(
        config,
        schedule=list(preregistration["pilot_schedule"]),
        progress=_progress,
    )
    provisional = build_result_artifact(
        source_sha=args.source_sha,
        generated_at=_utc_now(),
        preregistration=preregistration,
        trials=trials,
        throughput=[],
        execution_status="candidate",
        status_reason="pilot execution complete; eligibility evaluation pending",
        execution={"pilot": execution},
    )
    failed = _pilot_failure_reasons(provisional)
    status = "rejected" if failed else "candidate"
    reason = "; ".join(failed) if failed else "all pilot gates passed; full phase not yet executed"
    payload = build_result_artifact(
        source_sha=args.source_sha,
        generated_at=_utc_now(),
        preregistration=preregistration,
        trials=trials,
        throughput=[],
        execution_status=status,
        status_reason=reason,
        execution={"pilot": execution},
    )
    output = Path(classified_raw_artifact_path(args.output.as_posix(), status=status))
    _write_new(output, payload)
    print(json.dumps({"status": status, "output": str(output), "reason": reason}))
    return 0 if status == "candidate" else 2


def _full(args: argparse.Namespace) -> int:
    _require_clean_source(args.source_sha)
    preregistration = _read_preregistration(args.preregistration, source_sha=args.source_sha)
    pilot = json.loads(args.pilot_result.read_bytes())
    if not isinstance(pilot, dict):
        raise ValueError("pilot result must be a JSON object")
    validate_result_artifact(pilot, preregistration=preregistration)
    if pilot.get("status") != "candidate":
        raise RuntimeError("full execution requires a gate-passing candidate pilot")
    eligible = set(pilot["eligibility"]["advance_to_full"])
    schedule = [item for item in preregistration["full_schedule"] if item.get("arm") in eligible]
    if not schedule:
        raise RuntimeError("no pilot-eligible arms can advance to full execution")
    config = _config_from_preregistration(preregistration)
    full_trials, full_execution = run_candidate_phase(
        config,
        schedule=schedule,
        progress=_progress,
    )
    all_trials = [*pilot["trials"], *full_trials]
    full_counts = {
        arm: sum(trial.get("phase") == "full" and trial.get("arm") == arm for trial in all_trials)
        for arm in CANONICAL_ARMS
    }
    full_gate = eligible == set(CANONICAL_ARMS) and all(
        count == 30 for count in full_counts.values()
    )
    execution = dict(pilot.get("execution") or {})
    execution["full"] = full_execution
    lifecycle_result = build_result_artifact(
        source_sha=args.source_sha,
        generated_at=_utc_now(),
        preregistration=preregistration,
        trials=all_trials,
        throughput=[],
        execution_status="rejected",
        status_reason="full execution complete; lifecycle gates pending",
        execution=execution,
    )
    lifecycle_blocker = (
        lifecycle_gate_failure_reason(lifecycle_result, preregistration=preregistration)
        if full_gate
        else "full execution did not retain exactly 30 attempts for every pilot-eligible arm"
    )
    throughput: list[dict[str, Any]] = []
    throughput_failure: str | None = None
    if lifecycle_blocker is None:
        try:
            throughput = run_candidate_throughput(config)
        except Exception as exc:
            throughput_failure = f"throughput execution failed: {type(exc).__name__}: {exc}"
            execution["throughput_failure"] = {
                "error_type": type(exc).__name__,
                "reason": str(exc),
            }
    provisional = build_result_artifact(
        source_sha=args.source_sha,
        generated_at=lifecycle_result["generated_at"],
        preregistration=preregistration,
        trials=all_trials,
        throughput=throughput,
        execution_status="rejected",
        status_reason="full execution complete; promotion gates pending",
        execution=execution,
    )
    blocker = lifecycle_blocker or throughput_failure or promotion_gate_failure_reason(
        provisional, preregistration=preregistration
    )
    status = "complete" if blocker is None else "rejected"
    reason = (
        "all pilot, full lifecycle, throughput, verification, placement, and cleanup gates passed"
        if blocker is None
        else blocker
    )
    payload = build_result_artifact(
        source_sha=args.source_sha,
        generated_at=provisional["generated_at"],
        preregistration=preregistration,
        trials=all_trials,
        throughput=throughput,
        execution_status=status,
        status_reason=reason,
        execution=execution,
    )
    output = Path(classified_raw_artifact_path(args.output.as_posix(), status=status))
    _write_new(output, payload)
    print(json.dumps({"status": status, "output": str(output), "reason": reason}))
    return 0 if status == "complete" else 2


def _commands(source_sha: str) -> dict[str, str]:
    root = DEFAULT_ROOT.as_posix()
    tracked = "benchmark-data/modal-v2-candidate-results-2026-07-19.json"
    return {
        "preregister": (
            "uv run python scripts/run_modal_v2_candidate_benchmark.py preregister "
            f"--source-sha {source_sha} --image-revision {source_sha} "
            f"--output {root}/preregistration.json"
        ),
        "pilot": (
            "uv run python scripts/run_modal_v2_candidate_benchmark.py pilot "
            f"--source-sha {source_sha} --preregistration {root}/preregistration.json "
            f"--output {root}/candidates/pilot.json"
        ),
        "full": (
            "uv run python scripts/run_modal_v2_candidate_benchmark.py full "
            f"--source-sha {source_sha} --preregistration {root}/preregistration.json "
            f"--pilot-result {root}/candidates/pilot.json "
            f"--output {root}/candidates/full.json"
        ),
        "sanitize": (
            "uv run python scripts/sanitize_modal_v2_candidate_benchmark.py "
            f"{root}/candidates/full.json {tracked} "
            f"--preregistration {root}/preregistration.json "
            f"--raw-artifact-path {root}/candidates/full.json"
        ),
        "check": (
            "uv run python scripts/sanitize_modal_v2_candidate_benchmark.py "
            f"{root}/candidates/full.json {tracked} "
            f"--preregistration {root}/preregistration.json "
            f"--raw-artifact-path {root}/candidates/full.json --check"
        ),
    }


def _read_preregistration(path: Path, *, source_sha: str) -> dict[str, Any]:
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise ValueError("preregistration must be a JSON object")
    if payload.get("benchmark") != "modal-v2-candidate-preregistration":
        raise ValueError("preregistration benchmark name is invalid")
    if payload.get("source_sha") != source_sha:
        raise RuntimeError("preregistration source SHA differs from the clean harness")
    expected = build_preregistration(
        _config_from_preregistration(payload),
        source_sha=source_sha,
        generated_at=str(payload["generated_at"]),
        sdk_versions=dict(payload["environment"]["sdk_versions"]),
        package_versions=dict(payload["environment"]["package_versions"]),
        runner_identity=dict(payload["environment"]["runner_identity"]),
        commands=dict(payload["commands"]),
    )
    if preregistration_sha256(expected) != preregistration_sha256(payload):
        raise RuntimeError("preregistration content is not reproducible")
    return payload


def _config_from_preregistration(payload: dict[str, Any]) -> ModalV2CandidateConfig:
    configuration = dict(payload["configuration"])
    configuration["throughput_concurrency"] = tuple(configuration["throughput_concurrency"])
    return ModalV2CandidateConfig(**configuration)


def _pilot_failure_reasons(payload: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for arm, gate in payload["eligibility"]["arms"].items():
        if not gate["eligible"]:
            failures.extend(f"{arm}: {reason}" for reason in gate["reasons"])
    return failures


def _require_clean_source(source_sha: str) -> None:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required for benchmark provenance")
    if _git_output(git, "rev-parse", "HEAD") != source_sha:
        raise RuntimeError("source SHA does not match HEAD")
    if _git_output(git, "status", "--porcelain"):
        raise RuntimeError("credentialed benchmark execution requires a clean worktree")


def _git_output(git: str, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        [git, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _write_new(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise RuntimeError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_json(payload), encoding="utf-8")


def _progress(phase: str, completed: int, attempted: int, arm: str) -> None:
    print(
        json.dumps(
            {
                "status": "progress",
                "phase": phase,
                "completed": completed,
                "attempted": attempted,
                "arm": arm,
            }
        ),
        flush=True,
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

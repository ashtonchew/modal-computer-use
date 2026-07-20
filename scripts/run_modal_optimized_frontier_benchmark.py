from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import signal
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from modal_computer_use.benchmarks.modal_optimized_frontier import (
    PRIMARY_ARMS,
    OptimizedFrontierConfig,
    build_placement_binding,
    build_preregistration,
    build_result_artifact,
    classified_raw_artifact_path,
    lifecycle_gate_failure_reason,
    preregistration_sha256,
    promotion_gate_failure_reason,
    serialize_json,
    validate_result_artifact,
)
from modal_computer_use.benchmarks.modal_optimized_frontier_execution import (
    exclusive_frontier_execution_lock,
    raise_benchmark_termination_signal,
    run_frontier_phase,
    run_frontier_throughput,
)
from modal_computer_use.benchmarks.modal_v2_placement import (
    serialize_placement_capability,
)
from modal_computer_use.sandbox import cleanup_modal_benchmark_run

DEFAULT_ROOT = Path("benchmark-results/modal-optimized-frontier-2026-07-19")
PLACEMENT_ARTIFACT = Path("benchmark-data/modal-v2-placement-capability-2026-07-19.json")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the preregistered Modal optimized-frontier benchmark"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    preregister = subparsers.add_parser("preregister")
    preregister.add_argument("--source-sha", required=True)
    preregister.add_argument("--image-revision")
    preregister.add_argument("--placement-capability", type=Path, default=PLACEMENT_ARTIFACT)
    preregister.add_argument("--output", type=Path, default=DEFAULT_ROOT / "preregistration.json")
    preregister.add_argument("--bootstrap-resamples", type=int, default=2_000)
    preregister.add_argument("--max-estimated-cost-usd", type=float, default=20.0)

    pilot = subparsers.add_parser("pilot")
    _execution_arguments(pilot, DEFAULT_ROOT / "candidates/pilot.json")
    full = subparsers.add_parser("full")
    _execution_arguments(full, DEFAULT_ROOT / "candidates/full.json")
    full.add_argument("--pilot-result", type=Path, default=DEFAULT_ROOT / "candidates/pilot.json")
    args = parser.parse_args()
    if args.command == "preregister":
        return _preregister(args)
    signal.signal(signal.SIGTERM, raise_benchmark_termination_signal)
    with exclusive_frontier_execution_lock():
        if args.command == "pilot":
            return _pilot(args)
        return _full(args)


def _execution_arguments(parser: argparse.ArgumentParser, output: Path) -> None:
    parser.add_argument("--source-sha", required=True)
    parser.add_argument(
        "--preregistration", type=Path, default=DEFAULT_ROOT / "preregistration.json"
    )
    parser.add_argument("--output", type=Path, default=output)


def _preregister(args: argparse.Namespace) -> int:
    _require_clean_source(args.source_sha)
    raw = args.placement_capability.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or raw != serialize_placement_capability(payload):
        raise ValueError("placement capability must be canonical JSON")
    binding = build_placement_binding(
        payload,
        artifact_path=args.placement_capability.as_posix(),
        artifact_sha256=hashlib.sha256(raw).hexdigest(),
    )
    config = OptimizedFrontierConfig(
        image_revision=args.image_revision or args.source_sha,
        bootstrap_resamples=args.bootstrap_resamples,
        max_estimated_cost_usd=args.max_estimated_cost_usd,
    )
    preregistration = build_preregistration(
        config,
        source_sha=args.source_sha,
        generated_at=_utc_now(),
        placement_binding=binding,
        sdk_versions={"modal": version("modal")},
        package_versions={"modal-computer-use": version("modal-computer-use")},
        commands=_commands(args.source_sha),
    )
    _write_new(args.output, preregistration)
    print(json.dumps({"status": "preregistered", "output": str(args.output)}))
    return 0


def _pilot(args: argparse.Namespace) -> int:
    _require_clean_source(args.source_sha)
    preregistration = _read_preregistration(args.preregistration, args.source_sha)
    config = _config(preregistration)
    checkpoint_state: dict[str, Any] = {}
    checkpoint = _checkpoint_writer(
        _checkpoint_path(args.output, "pilot"),
        preregistration=preregistration,
        phase="pilot",
        schedule_total=len(preregistration["pilot_schedule"]),
        state=checkpoint_state,
    )
    try:
        trials, execution = run_frontier_phase(
            config,
            schedule=list(preregistration["pilot_schedule"]),
            progress=_progress,
            checkpoint=checkpoint,
        )
    except BaseException as exc:
        return _failed_phase(
            args=args,
            preregistration=preregistration,
            phase="pilot",
            checkpoint_state=checkpoint_state,
            error=exc,
        )
    provisional = build_result_artifact(
        source_sha=args.source_sha,
        generated_at=_utc_now(),
        preregistration=preregistration,
        trials=trials,
        throughput=[],
        execution_status="candidate",
        status_reason="pilot complete; primary eligibility pending",
        execution={"pilot": execution},
    )
    eligible = provisional["eligibility"]["primary_pilot_eligible"] is True
    status = "candidate" if eligible else "rejected"
    reason = (
        "both primary pilot arms passed; full phase is eligible"
        if eligible
        else "; ".join(
            [
                *(
                    f"{arm}: {gate_reason}"
                    for arm in PRIMARY_ARMS
                    for gate_reason in provisional["eligibility"]["arms"][arm]["reasons"]
                ),
                *(
                    gate_reason
                    for gate_reason in provisional["eligibility"]["comparison"]["reasons"]
                    if gate_reason != "one or more primary pilot arm gates failed"
                ),
            ]
        )
    )
    result = build_result_artifact(
        source_sha=args.source_sha,
        generated_at=provisional["generated_at"],
        preregistration=preregistration,
        trials=trials,
        throughput=[],
        execution_status=status,
        status_reason=reason,
        execution={"pilot": execution},
    )
    output = Path(classified_raw_artifact_path(args.output.as_posix(), status=status))
    _write_new(output, result)
    print(json.dumps({"status": status, "output": str(output), "reason": reason}))
    return 0 if eligible else 2


def _full(args: argparse.Namespace) -> int:
    _require_clean_source(args.source_sha)
    preregistration = _read_preregistration(args.preregistration, args.source_sha)
    pilot = json.loads(args.pilot_result.read_bytes())
    if not isinstance(pilot, dict):
        raise ValueError("pilot result must be an object")
    validate_result_artifact(pilot, preregistration=preregistration)
    if (
        pilot.get("status") != "candidate"
        or pilot["eligibility"].get("primary_pilot_eligible") is not True
    ):
        raise RuntimeError("full requires a gate-passing primary pilot")
    config = _config(preregistration)
    checkpoint_state: dict[str, Any] = {}
    checkpoint = _checkpoint_writer(
        _checkpoint_path(args.output, "full"),
        preregistration=preregistration,
        phase="full",
        schedule_total=len(preregistration["full_schedule"]),
        state=checkpoint_state,
    )
    try:
        trials, execution = run_frontier_phase(
            config,
            schedule=list(preregistration["full_schedule"]),
            progress=_progress,
            checkpoint=checkpoint,
        )
    except BaseException as exc:
        return _failed_phase(
            args=args,
            preregistration=preregistration,
            phase="full",
            checkpoint_state=checkpoint_state,
            error=exc,
            prior=pilot,
        )
    all_trials = [*pilot["trials"], *trials]
    all_execution = dict(pilot["execution"])
    all_execution["full"] = execution
    lifecycle = build_result_artifact(
        source_sha=args.source_sha,
        generated_at=_utc_now(),
        preregistration=preregistration,
        trials=all_trials,
        throughput=[],
        execution_status="rejected",
        status_reason="full lifecycle complete; lifecycle gates pending",
        execution=all_execution,
    )
    blocker = lifecycle_gate_failure_reason(lifecycle, preregistration=preregistration)
    throughput: list[dict[str, Any]] = []
    if blocker is None:
        throughput_run_id = f"{execution['run_id']}-throughput"
        try:
            throughput, throughput_cleanup = run_frontier_throughput(
                config,
                run_id=throughput_run_id,
            )
            all_execution["throughput_cleanup"] = throughput_cleanup
        except BaseException as exc:
            cleanup = _cleanup_run(
                app_name="modal-computer-use-optimized-frontier-throughput",
                run_id=throughput_run_id,
            )
            all_execution["throughput_cleanup"] = cleanup
            all_execution["throughput_failure"] = {"error_type": type(exc).__name__}
            blocker = f"throughput failed after lifecycle eligibility: {type(exc).__name__}"
    provisional = build_result_artifact(
        source_sha=args.source_sha,
        generated_at=lifecycle["generated_at"],
        preregistration=preregistration,
        trials=all_trials,
        throughput=throughput,
        execution_status="rejected",
        status_reason="full execution complete; promotion gates pending",
        execution=all_execution,
    )
    blocker = blocker or promotion_gate_failure_reason(provisional, preregistration=preregistration)
    status = "complete" if blocker is None else "rejected"
    reason = (
        "all primary pilot, full lifecycle, throughput, verification, provenance, cost, and "
        "cleanup gates passed"
        if blocker is None
        else blocker
    )
    result = build_result_artifact(
        source_sha=args.source_sha,
        generated_at=provisional["generated_at"],
        preregistration=preregistration,
        trials=all_trials,
        throughput=throughput,
        execution_status=status,
        status_reason=reason,
        execution=all_execution,
    )
    output = Path(classified_raw_artifact_path(args.output.as_posix(), status=status))
    _write_new(output, result)
    print(json.dumps({"status": status, "output": str(output), "reason": reason}))
    return 0 if status == "complete" else 2


def _failed_phase(
    *,
    args: argparse.Namespace,
    preregistration: dict[str, Any],
    phase: str,
    checkpoint_state: dict[str, Any],
    error: BaseException,
    prior: dict[str, Any] | None = None,
) -> int:
    execution = copy.deepcopy(checkpoint_state.get("execution") or {})
    run_id = execution.get("run_id")
    app_name = execution.get("app_name")
    cleanup = (
        _cleanup_run(app_name=app_name, run_id=run_id)
        if isinstance(app_name, str) and isinstance(run_id, str)
        else _empty_failed_cleanup("MissingRunIdentity")
    )
    execution["state"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
    execution["error_type"] = type(error).__name__
    execution["run_cleanup"] = cleanup
    trials = [*((prior or {}).get("trials") or []), *(checkpoint_state.get("trials") or [])]
    all_execution = dict((prior or {}).get("execution") or {})
    all_execution[phase] = execution
    reason = (
        f"{phase} interrupted after {len(checkpoint_state.get('trials') or [])} retained attempts"
        if isinstance(error, KeyboardInterrupt)
        else f"{phase} execution failed: {type(error).__name__}"
    )
    result = build_result_artifact(
        source_sha=args.source_sha,
        generated_at=_utc_now(),
        preregistration=preregistration,
        trials=trials,
        throughput=[],
        execution_status="rejected",
        status_reason=reason,
        execution=all_execution,
    )
    output = Path(classified_raw_artifact_path(args.output.as_posix(), status="rejected"))
    _write_new(output, result)
    print(json.dumps({"status": "rejected", "output": str(output), "reason": reason}))
    return 130 if isinstance(error, KeyboardInterrupt) else 2


def _read_preregistration(path: Path, source_sha: str) -> dict[str, Any]:
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict) or payload.get("benchmark") != (
        "modal-optimized-frontier-preregistration"
    ):
        raise ValueError("preregistration benchmark is invalid")
    if payload.get("source_sha") != source_sha:
        raise RuntimeError("preregistration source differs from clean HEAD")
    expected = build_preregistration(
        _config(payload),
        source_sha=source_sha,
        generated_at=payload["generated_at"],
        placement_binding=dict(payload["environment"]["placement_capability"]),
        sdk_versions=dict(payload["environment"]["sdk_versions"]),
        package_versions=dict(payload["environment"]["package_versions"]),
        commands=dict(payload["commands"]),
    )
    if preregistration_sha256(expected) != preregistration_sha256(payload):
        raise RuntimeError("preregistration is not reproducible")
    return payload


def _config(payload: dict[str, Any]) -> OptimizedFrontierConfig:
    values = dict(payload["configuration"])
    values["throughput_concurrency"] = tuple(values["throughput_concurrency"])
    return OptimizedFrontierConfig(**values)


def _commands(source_sha: str) -> dict[str, str]:
    root = DEFAULT_ROOT.as_posix()
    tracked = "benchmark-data/modal-optimized-frontier-results-2026-07-19.json"
    script = "scripts/run_modal_optimized_frontier_benchmark.py"
    sanitizer = "scripts/sanitize_modal_optimized_frontier_benchmark.py"
    return {
        "preregister": f"uv run python {script} preregister --source-sha {source_sha}",
        "pilot": f"uv run python {script} pilot --source-sha {source_sha}",
        "full": f"uv run python {script} full --source-sha {source_sha}",
        "sanitize": (
            f"uv run python {sanitizer} {root}/candidates/full.json {tracked} "
            f"--preregistration {root}/preregistration.json "
            f"--raw-artifact-path {root}/candidates/full.json"
        ),
        "check": (
            f"uv run python {sanitizer} {root}/candidates/full.json {tracked} "
            f"--preregistration {root}/preregistration.json "
            f"--raw-artifact-path {root}/candidates/full.json --check"
        ),
    }


def _require_clean_source(source_sha: str) -> None:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required")
    if _git_output(git, "rev-parse", "HEAD") != source_sha:
        raise RuntimeError("source SHA does not match HEAD")
    if _git_output(git, "status", "--porcelain"):
        raise RuntimeError("credentialed execution requires a clean worktree")


def _git_output(git: str, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        [git, *args], check=True, capture_output=True, text=True, timeout=10
    ).stdout.strip()


def _checkpoint_writer(
    path: Path,
    *,
    preregistration: dict[str, Any],
    phase: str,
    schedule_total: int,
    state: dict[str, Any],
) -> Any:
    def write(trials: list[dict[str, Any]], execution: dict[str, Any]) -> None:
        payload = {
            "schema_version": 1,
            "benchmark": "modal-optimized-frontier-checkpoint",
            "generated_at": _utc_now(),
            "source_sha": preregistration["source_sha"],
            "preregistration_sha256": preregistration_sha256(preregistration),
            "phase": phase,
            "state": execution.get("state"),
            "schedule_total": schedule_total,
            "completed_attempts": len(trials),
            "trials": copy.deepcopy(trials),
            "execution": copy.deepcopy(execution),
        }
        _write_atomic(path, payload)
        state.clear()
        state.update(copy.deepcopy(payload))

    return write


def _checkpoint_path(output: Path, phase: str) -> Path:
    if not output.parts or output.parts[0] != "benchmark-results":
        raise ValueError("output must be under benchmark-results")
    root = (
        output.parent.parent if output.parent.name in {"candidates", "rejected"} else output.parent
    )
    return root / "checkpoints" / f"{phase}.json"


def _cleanup_run(*, app_name: str, run_id: str) -> dict[str, Any]:
    try:
        return cleanup_modal_benchmark_run(
            app_name=app_name,
            run_id=run_id,
            include_inventory=True,
        )
    except Exception as exc:
        return _empty_failed_cleanup(type(exc).__name__)


def _empty_failed_cleanup(error_type: str) -> dict[str, Any]:
    return {
        "matched_sandboxes": None,
        "terminated_sandboxes": 0,
        "termination_failures": None,
        "remaining_sandboxes": None,
        "cleanup_succeeded": False,
        "error_type": error_type,
        "enumeration": None,
    }


def _progress(*values: Any) -> None:
    phase, completed, attempted, arm, trial_status, error_type = values
    print(
        json.dumps(
            {
                "status": "progress",
                "phase": phase,
                "completed": completed,
                "attempted": attempted,
                "arm": arm,
                "trial_status": trial_status,
                "error_type": error_type,
            }
        ),
        flush=True,
    )


def _write_new(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise RuntimeError(f"output already exists: {path}")
    _write_atomic(path, payload)


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(serialize_json(payload), encoding="utf-8")
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

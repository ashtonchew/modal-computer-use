from __future__ import annotations

import asyncio
import fcntl
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config import (
    ActionConfig,
    BrowserConfig,
    ComputerConfig,
    ImageConfig,
    ResourceConfig,
    RuntimeConfig,
)
from ..latency import SessionStartupTiming
from ..sandbox import (
    ModalBenchmarkRunner,
    cleanup_modal_benchmark_run,
    create_modal_benchmark_allocation_context,
    create_modal_benchmark_computer,
    create_modal_benchmark_runner,
)
from ..state import new_run_id
from .modal_optimized_frontier import (
    ARM_V1_CONNECT,
    ARM_V1_TUNNEL,
    ARM_V2_I6PN,
    PRIMARY_ARMS,
    OptimizedFrontierConfig,
    arm_definitions,
    dual_list_cleanup_inventory_passed,
    requested_controls,
)
from .modal_v2_candidate_execution import (
    empty_direct_runner_verification,
    extract_modal_direct_runner_result,
    modal_direct_runner_code,
    observed_startup_stage_ms,
)

FRONTIER_RESULT_START = "__MODAL_OPTIMIZED_FRONTIER_RESULT_START__"
FRONTIER_RESULT_END = "__MODAL_OPTIMIZED_FRONTIER_RESULT_END__"
DEFAULT_EXECUTION_LOCK = Path(tempfile.gettempdir()) / "modal-computer-use-optimized-frontier.lock"


class BenchmarkTerminationSignal(KeyboardInterrupt):
    """Turn process termination into the benchmark's cleanup/checkpoint path."""


def raise_benchmark_termination_signal(_signum: int, _frame: Any) -> None:
    raise BenchmarkTerminationSignal


@contextmanager
def exclusive_frontier_execution_lock(
    lock_path: Path = DEFAULT_EXECUTION_LOCK,
) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another optimized-frontier execution is already active") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def run_frontier_phase(
    config: OptimizedFrontierConfig,
    *,
    schedule: list[dict[str, Any]],
    app_name: str = "modal-computer-use-optimized-frontier",
    progress: Any | None = None,
    checkpoint: Any | None = None,
    trial_runner: Any | None = None,
    cleanup_sweep: Any = cleanup_modal_benchmark_run,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    run_id = new_run_id()
    trials: list[dict[str, Any]] = []
    state = "starting"
    error_type: str | None = None
    final_cleanup: dict[str, Any] | None = None
    execute_trial = trial_runner or run_frontier_trial
    try:
        if checkpoint is not None:
            checkpoint(trials, _phase_execution(run_id, app_name, state=state))
        state = "running"
        for item in schedule:
            trial = execute_trial(
                config,
                schedule_item=item,
                app_name=app_name,
                phase_run_id=run_id,
                cleanup_sweep=cleanup_sweep,
            )
            trials.append(trial)
            if checkpoint is not None:
                checkpoint(trials, _phase_execution(run_id, app_name, state=state))
            if progress is not None:
                failure = trial.get("failure")
                progress(
                    item["phase"],
                    len(trials),
                    len(schedule),
                    item["arm"],
                    trial.get("status"),
                    failure.get("error_type") if isinstance(failure, dict) else None,
                )
    except BaseException as exc:
        error_type = type(exc).__name__
        state = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        raise
    finally:
        try:
            final_cleanup = cleanup_sweep(
                app_name=app_name,
                run_id=run_id,
                include_inventory=True,
            )
        except Exception as exc:
            final_cleanup = _failed_cleanup(exc)
        if checkpoint is not None:
            checkpoint(
                trials,
                _phase_execution(
                    run_id,
                    app_name,
                    state="complete" if error_type is None else state,
                    error_type=error_type,
                    run_cleanup=final_cleanup,
                ),
            )
    return trials, _phase_execution(
        run_id,
        app_name,
        state="complete",
        run_cleanup=final_cleanup,
    )


def run_frontier_trial(
    config: OptimizedFrontierConfig,
    *,
    schedule_item: dict[str, Any],
    app_name: str,
    phase_run_id: str,
    cleanup_sweep: Any = cleanup_modal_benchmark_run,
    runner_factory: Any = create_modal_benchmark_runner,
    target_factory: Any = create_modal_benchmark_computer,
    clock: Any = time.perf_counter,
) -> dict[str, Any]:
    arm = str(schedule_item["arm"])
    definition = arm_definitions().get(arm)
    if definition is None:
        raise ValueError("schedule contains an unsupported optimized-frontier arm")
    backend = str(definition["backend_generation"])
    transport = str(definition["ingress"])
    lifecycle_run_id = f"{phase_run_id}-{schedule_item['phase']}-{schedule_item['sequence']:03d}"
    runner: ModalBenchmarkRunner | None = None
    target: Any | None = None
    timing = SessionStartupTiming(clock=clock)
    lifecycle_started = clock()
    target_started: float | None = None
    runner_terminated = False
    target_terminated = False
    target_detached = False
    cleanup: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    failure_stage = "runner_create"
    status = "failed"
    metrics: dict[str, float | None] = {
        metric: None
        for metric in (
            "allocation_ms",
            "daemon_ready_ms",
            "browser_ready_ms",
            "first_valid_frame_ms",
            "warm_action_to_frame_ms",
        )
    }
    expected_cloud, expected_region = config.expected_placement(arm)
    actual: dict[str, Any] = {
        "target_cloud": None,
        "target_region": None,
        "runner_cloud": None,
        "runner_region": None,
        "i6pn_reachability": None,
    }
    verification = _frontier_empty_verification()
    try:
        runner = runner_factory(
            app_name=app_name,
            cloud=config.requested_cloud(arm),
            region=config.region,
            image_revision=config.image_revision,
            cpu=config.runner_cpu,
            memory_mib=config.runner_memory_mib,
            backend=backend,
            i6pn=arm == ARM_V2_I6PN,
            app_tags={"benchmark": "modal-optimized-frontier"},
            tags={
                "benchmark_run": lifecycle_run_id,
                "benchmark_arm": arm,
            },
            runner_label="modal-optimized-frontier",
        )
        actual["runner_cloud"] = runner.placement.get("cloud")
        actual["runner_region"] = runner.placement.get("region")
        verification["runner_placement"] = (actual["runner_cloud"], actual["runner_region"]) == (
            expected_cloud,
            expected_region,
        )
        if not verification["runner_placement"]:
            raise RuntimeError("runner placement differed from the predeclared frontier")
        failure_stage = "target_create"
        target_started = clock()
        target = target_factory(
            config=_target_config(
                config,
                arm=arm,
                lifecycle_run_id=lifecycle_run_id,
            ),
            backend=backend,
            transport=transport,
            cloud=config.requested_cloud(arm),
            app_name=app_name,
            app_tags={"benchmark": "modal-optimized-frontier"},
            tags={"benchmark_arm": arm},
            wait=True,
            timing=timing,
        )
        stages = timing.as_dict()["stages"]
        metrics["allocation_ms"] = observed_startup_stage_ms(stages, "sandbox_registered")
        failure_stage = "target_placement"
        placement = target.runtime_placement()
        actual["target_cloud"] = placement["cloud"]
        actual["target_region"] = placement["region"]
        if (placement["cloud"], placement["region"]) != (expected_cloud, expected_region):
            raise RuntimeError("target placement differed from the predeclared frontier")
        failure_stage = "direct_runner"
        dispatch_offset_ms = (clock() - target_started) * 1000.0
        runner_result = runner.execute(
            target,
            (
                "python",
                "-c",
                modal_direct_runner_code(
                    result_start=FRONTIER_RESULT_START,
                    result_end=FRONTIER_RESULT_END,
                    source_prefix="modal-optimized-frontier",
                ),
            ),
            transport=transport,
            timeout_seconds=config.readiness_timeout_seconds + 120,
        )
        payload = extract_modal_direct_runner_result(
            runner_result.stdout,
            result_start=FRONTIER_RESULT_START,
            result_end=FRONTIER_RESULT_END,
        )
        if payload.get("status") != "valid":
            raise RuntimeError(str(payload.get("error_type") or "frontier runner failed"))
        failure_stage = "result_validation"
        stages_ms = payload["stages_ms"]
        metrics.update(
            {
                "daemon_ready_ms": dispatch_offset_ms + float(stages_ms["daemon_ready"]),
                "browser_ready_ms": dispatch_offset_ms + float(stages_ms["browser_ready"]),
                "first_valid_frame_ms": dispatch_offset_ms + float(stages_ms["first_valid_frame"]),
                "warm_action_to_frame_ms": float(payload["warm_action_to_frame_ms"]),
            }
        )
        verification.update(dict(payload["verification"]))
        actual["runner_cloud"] = payload["placement"].get("cloud")
        actual["runner_region"] = payload["placement"].get("region")
        verification["runner_placement"] = (actual["runner_cloud"], actual["runner_region"]) == (
            expected_cloud,
            expected_region,
        )
        actual["i6pn_reachability"] = (
            "verified-workspace-private-direct" if arm == ARM_V2_I6PN else "not-applicable"
        )
        if not verification["runner_placement"]:
            raise RuntimeError("measured runner placement differed from the predeclared frontier")
        status = "valid"
    except Exception as exc:
        stages = timing.as_dict()["stages"]
        observed_allocation_ms = observed_startup_stage_ms(stages, "sandbox_registered")
        if metrics["allocation_ms"] is None and observed_allocation_ms is not None:
            metrics["allocation_ms"] = observed_allocation_ms
        if failure_stage == "target_create" and observed_allocation_ms is not None:
            failure_stage = (
                "target_authenticated_readiness"
                if observed_startup_stage_ms(stages, "container_ready") is not None
                else "target_container_readiness"
            )
        failure = {
            "phase": "lifecycle",
            "stage": failure_stage,
            "error_type": type(exc).__name__,
        }
        status = "timeout" if isinstance(exc, TimeoutError) else "failed"
    finally:
        if target is not None:
            try:
                target.terminate(wait=True)
            except Exception:
                target_terminated = False
            else:
                target_terminated = True
            try:
                target.detach()
            except Exception:
                target_detached = False
            else:
                target_detached = True
        target_duration_seconds = (
            0.0 if target_started is None else max(0.0, clock() - target_started)
        )
        if runner is not None:
            runner_terminated = runner.terminate()
        runner_duration_seconds = max(0.0, clock() - lifecycle_started)
        try:
            cleanup = cleanup_sweep(
                app_name=app_name,
                run_id=lifecycle_run_id,
                include_inventory=True,
            )
        except Exception as exc:
            cleanup = _failed_cleanup(exc)
    sweep_succeeded = _cleanup_succeeded(cleanup)
    if not (target_terminated and target_detached and runner_terminated and sweep_succeeded):
        status = "failed"
        if failure is None:
            failure = {"phase": "cleanup", "error_type": "CleanupIncomplete"}
    return {
        "sequence": schedule_item["sequence"],
        "phase": schedule_item["phase"],
        "arm": arm,
        "lifecycle_index": schedule_item["lifecycle_index"],
        "status": status,
        "metrics": metrics,
        "requested": requested_controls(config, arm),
        "actual": actual,
        "verification": verification,
        "retry_count": 0,
        "failure": failure,
        "cleanup": {
            "target_terminated": target_terminated,
            "target_detached": target_detached,
            "runner_terminated": runner_terminated,
            "run_sweep_succeeded": sweep_succeeded,
            "enumeration": None if cleanup is None else cleanup.get("enumeration"),
        },
        "resource_duration_seconds": {
            "target": target_duration_seconds,
            "runner": runner_duration_seconds,
        },
        "estimated_billed_cost": _estimated_cost(
            config,
            target_duration_seconds=target_duration_seconds,
            runner_duration_seconds=runner_duration_seconds,
        ),
    }


def run_frontier_throughput(
    config: OptimizedFrontierConfig,
    *,
    run_id: str,
    app_name: str = "modal-computer-use-optimized-frontier-throughput",
    context_factory: Any = create_modal_benchmark_allocation_context,
    cleanup_sweep: Any = cleanup_modal_benchmark_run,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not run_id:
        raise ValueError("throughput requires an exact run ID")
    total = len(PRIMARY_ARMS) * sum(config.throughput_concurrency)
    rate = config.cpu * 0.00003942 + (config.memory_mib / 1024) * 0.00000667
    ceiling = total * config.sandbox_timeout_seconds * rate * 1.75
    if ceiling > config.max_estimated_cost_usd:
        raise RuntimeError("throughput cost ceiling exceeds preregistration")
    contexts = {
        ARM_V1_TUNNEL: context_factory(
            app_name=app_name,
            image_revision=config.image_revision,
            run_id=run_id,
            cloud=config.v1_cloud,
            region=config.region,
            cpu=config.cpu,
            memory_mib=config.memory_mib,
            benchmark_tag="modal-optimized-frontier-throughput",
        ),
        ARM_V2_I6PN: context_factory(
            app_name=app_name,
            image_revision=config.image_revision,
            run_id=run_id,
            cloud=config.v2_cloud,
            region=config.region,
            cpu=config.cpu,
            memory_mib=config.memory_mib,
            benchmark_tag="modal-optimized-frontier-throughput",
        ),
    }

    async def execute() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for concurrency in config.throughput_concurrency:
            for arm in PRIMARY_ARMS:
                backend = "v1" if arm == ARM_V1_TUNNEL else "v2"
                row = await contexts[arm].run_batch(
                    backend=backend,
                    concurrency=concurrency,
                    timeout_seconds=config.sandbox_timeout_seconds,
                )
                row.update(
                    {
                        "arm": arm,
                        "classification": "optimized-frontier-allocation-throughput",
                        "requested_cloud": config.requested_cloud(arm),
                        "requested_region": config.region,
                    }
                )
                rows.append(row)
        return rows

    try:
        rows = asyncio.run(execute())
    finally:
        cleanup = cleanup_sweep(
            app_name=app_name,
            run_id=run_id,
            include_inventory=True,
        )
    return rows, cleanup


def _target_config(
    config: OptimizedFrontierConfig, *, arm: str, lifecycle_run_id: str
) -> ComputerConfig:
    return ComputerConfig(
        runtime=RuntimeConfig(
            timeout_seconds=config.sandbox_timeout_seconds,
            readiness_timeout_seconds=config.readiness_timeout_seconds,
            modal_region=config.region,
        ),
        resources=ResourceConfig(
            profile="browser",
            cpu=config.cpu,
            memory_mib=config.memory_mib,
        ),
        image=ImageConfig(source="named", revision=config.image_revision),
        browser=BrowserConfig(kind="chromium", prewarm=True),
        actions=ActionConfig(input_rate_limit_per_sec=0),
        run_id=lifecycle_run_id,
        ingress="connect" if arm == ARM_V1_CONNECT else "tunnel",
    )


def _phase_execution(
    run_id: str,
    app_name: str,
    *,
    state: str,
    error_type: str | None = None,
    run_cleanup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "state": state,
        "error_type": error_type,
        "run_id": run_id,
        "app_name": app_name,
        "runner_lifecycle": "independent-per-sample",
        "target_lifecycle": "independent-per-sample",
        "broker_on_action_or_frame_path": False,
        "run_cleanup": None if run_cleanup is None else dict(run_cleanup),
    }


def _frontier_empty_verification() -> dict[str, bool]:
    return {"runner_placement": False, **empty_direct_runner_verification()}


def _estimated_cost(
    config: OptimizedFrontierConfig,
    *,
    target_duration_seconds: float,
    runner_duration_seconds: float,
) -> dict[str, Any]:
    target_rate = config.cpu * 0.00003942 + (config.memory_mib / 1024) * 0.00000667
    runner_rate = config.runner_cpu * 0.00003942 + (config.runner_memory_mib / 1024) * 0.00000667
    estimated = 1.75 * (
        target_duration_seconds * target_rate + runner_duration_seconds * runner_rate
    )
    return {
        "status": "resource-time-proxy",
        "estimated_usd": estimated,
        "target_duration_seconds": target_duration_seconds,
        "runner_duration_seconds": runner_duration_seconds,
        "region_multiplier": 1.75,
        "included": ["target_cpu", "target_memory", "runner_cpu", "runner_memory"],
    }


def _cleanup_succeeded(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        value.get("cleanup_succeeded") is True
        and value.get("remaining_sandboxes") == 0
        and value.get("termination_failures") == 0
        and dual_list_cleanup_inventory_passed(value.get("enumeration"))
    )


def _failed_cleanup(exc: Exception) -> dict[str, Any]:
    return {
        "matched_sandboxes": None,
        "terminated_sandboxes": 0,
        "termination_failures": None,
        "remaining_sandboxes": None,
        "cleanup_succeeded": False,
        "error_type": type(exc).__name__,
        "enumeration": None,
    }

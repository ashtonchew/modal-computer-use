from __future__ import annotations

import hashlib
import time
from typing import Any

from ..config import (
    ActionConfig,
    BrowserConfig,
    ComputerConfig,
    ImageConfig,
    ResourceConfig,
    RuntimeConfig,
)
from ..latency import SessionStartupTiming, WarmPoolPolicy
from ..manager import ComputerSandboxManager
from ..sandbox import ComputerSandbox
from ..state import new_run_id
from .modal_colocated_client import (
    ModalColocatedClientBenchmarkConfig,
    ModalColocatedRunnerPath,
    run_modal_colocated_runner_benchmark,
)
from .modal_optimization import (
    OPTIMIZED_ACTION_CASE,
    PROFILE_MODAL_ON_DEMAND,
    ModalOptimizationConfig,
    _attempt_row,
    _is_nonnegative_finite,
    _mapping,
    _nested_number,
    action_attempts_from_case,
)


def run_independent_cold_attempts(
    config: ModalOptimizationConfig,
    *,
    create_computer: Any = ComputerSandbox.create,
    clock: Any = time.perf_counter,
    progress: Any | None = None,
    profile: str = PROFILE_MODAL_ON_DEMAND,
    progress_label: str = "cold",
) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for index in range(config.cold_attempts):
        timing = SessionStartupTiming(clock=clock)
        computer: Any | None = None
        computer_config = modal_optimization_computer_config(
            config,
            run_id=f"modal-opt-cold-{index:03d}-{new_run_id()}",
        )
        resource_started = clock()
        try:
            computer = create_computer(
                config=computer_config,
                app_name="modal-computer-use",
                app_tags={"benchmark": profile},
                tags={"benchmark": profile, "role": "cold-target"},
                wait=True,
                timing=timing,
            )
            computer.ensure_browser_ready(computer_config, timing=timing)
            computer.first_valid_frame(computer_config, timing=timing)
            actual_region = computer.runtime_region()
            stages = timing.as_dict()["stages"]
            elapsed_ms = stages["first_valid_frame"]["elapsed_ms"]
            attempt = _attempt_row(index, status="valid", elapsed_ms=elapsed_ms)
            attempt.update(
                {
                    "stages": stages,
                    "requested_placement": config.region,
                    "actual_placement": actual_region,
                    "resource_duration_seconds": max(0.0, clock() - resource_started),
                }
            )
        except Exception as exc:
            status = "timeout" if isinstance(exc, TimeoutError) else "failed"
            attempt = _attempt_row(
                index,
                status=status,
                failure={"phase": "cold_readiness", "error_type": type(exc).__name__},
            )
            attempt.update(
                {
                    "stages": timing.as_dict()["stages"],
                    "requested_placement": config.region,
                    "actual_placement": None,
                    "resource_duration_seconds": max(0.0, clock() - resource_started),
                }
            )
        attempt["cleanup"] = _cleanup_computer(computer)
        attempts.append(attempt)
        if progress is not None:
            progress(progress_label, len(attempts), config.cold_attempts)
    return attempts


def run_warm_action_attempts(
    config: ModalOptimizationConfig,
    *,
    create_computer: Any = ComputerSandbox.create,
    runner_benchmark: Any = run_modal_colocated_runner_benchmark,
    clock: Any = time.perf_counter,
    progress: Any | None = None,
    profile: str = PROFILE_MODAL_ON_DEMAND,
    runner_path: ModalColocatedRunnerPath = "connect",
    progress_label: str = "warm_action",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    run_id = new_run_id()
    computer_config = modal_optimization_computer_config(
        config,
        run_id=f"{run_id}-target",
    )
    computer: Any | None = None
    started = clock()
    cleanup: dict[str, Any]
    try:
        computer = create_computer(
            config=computer_config,
            app_name="modal-computer-use",
            app_tags={"benchmark": profile, "benchmark_run_id": run_id},
            tags={
                "benchmark": profile,
                "benchmark_run_id": run_id,
                "role": "warm-action-target",
            },
            wait=True,
        )
        computer.ensure_browser_ready(computer_config)
        computer.first_valid_frame(computer_config)
        actual_region = computer.runtime_region()
        metadata = computer.metadata()
        target_id = None if metadata is None else metadata.sandbox_id
        runner_config = ModalColocatedClientBenchmarkConfig(
            app_name="modal-computer-use",
            name=None,
            target_config_factory=lambda _run_id: computer_config,
            modal_region=config.region,
            caller_region_label=None,
            modal_ingress=config.ingress,
            daemon_http_version="1.1",
            resource_profile="browser",
            browser=config.browser,
            gpu=None,
            modal_cpu=config.cpu,
            modal_memory_mib=config.memory_mib,
            runner_cpu=1.0,
            runner_memory_mib=1024,
            input_rate_limit_per_sec=0,
            image_profile=f"named:{config.image_revision}",
            surfaces=["daemon-observation-stream"],
            observation_cases=[OPTIMIZED_ACTION_CASE],
            runner_paths=[runner_path],
            iterations=config.warm_action_attempts,
        )
        result = runner_benchmark(
            runner_config,
            run_id=run_id,
            runner_path=runner_path,
            computer=computer,
            target_sandbox_id=target_id,
        )
        case = _runner_action_case(result)
        attempts = action_attempts_from_case(
            case,
            expected_attempts=config.warm_action_attempts,
        )
        duration = max(0.0, clock() - started)
    except Exception as exc:
        duration = max(0.0, clock() - started)
        status = "timeout" if isinstance(exc, TimeoutError) else "failed"
        attempts = [
            _attempt_row(
                index,
                status=status,
                failure={"phase": "warm_action_run", "error_type": type(exc).__name__},
            )
            for index in range(config.warm_action_attempts)
        ]
        actual_region = None
    finally:
        cleanup = _cleanup_computer(computer)
    for attempt in attempts:
        attempt["requested_placement"] = config.region
        attempt["actual_placement"] = actual_region
        attempt["cleanup"] = {
            "attempted": False,
            "succeeded": None,
            "error_type": None,
            "reason": "persistent target cleanup is recorded at profile scope",
        }
    if progress is not None:
        progress(progress_label, len(attempts), config.warm_action_attempts)
    return attempts, {
        "target_cleanup": cleanup,
        "target_resource_duration_seconds": duration,
        "runner_path": f"same-region-separate-modal-runner:{runner_path}",
        "target_loopback": False,
        "action_transport": "persistent-hot-session",
        "observation_transport": "binary-envelope-causal-observation",
    }


def run_warm_claim_attempts(
    config: ModalOptimizationConfig,
    *,
    manager: ComputerSandboxManager | None = None,
    sleep: Any = time.sleep,
    progress: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pool_manager = manager or ComputerSandboxManager()
    pool_name = f"modal-opt-{config.image_revision[:8]}-{new_run_id()[:8]}"
    policy = WarmPoolPolicy(pool_name=pool_name, capacity=config.warm_pool_target)
    computer_config = modal_optimization_computer_config(
        config,
        run_id=f"pool-{pool_name}",
    )
    attempts: list[dict[str, Any]] = []
    fill_failures: list[dict[str, Any]] = []
    cleanup: dict[str, Any]
    try:
        while len(attempts) < config.warm_claim_attempts:
            try:
                pool_manager.fill_warm_pool(config=computer_config, policy=policy)
            except Exception as exc:
                fill_failures.append(
                    {"phase": "pool_fill", "error_type": type(exc).__name__}
                )
            sleep(config.warm_idle_seconds)
            batch = min(
                config.warm_pool_target,
                config.warm_claim_attempts - len(attempts),
            )
            for _ in range(batch):
                index = len(attempts)
                claim: Any | None = None
                try:
                    claim = pool_manager.claim_warm_pool(config=computer_config, policy=policy)
                    metrics = claim.metrics
                    elapsed = metrics.request_to_first_frame_ms
                    if not _is_nonnegative_finite(elapsed):
                        raise ValueError("warm claim did not report a valid first-frame boundary")
                    attempt = _attempt_row(index, status="valid", elapsed_ms=float(elapsed))
                    cost = metrics.cost_accounting or {}
                    attempt.update(
                        {
                            "request_to_authenticated_ms": metrics.request_to_authenticated_ms,
                            "claim_elapsed_ms": metrics.claim_elapsed_ms,
                            "pool_hit": metrics.hit,
                            "pool_miss": not metrics.hit,
                            "cold_fallback": metrics.cold_fallback,
                            "miss_reason": metrics.miss_reason,
                            "rejection_reasons": list(metrics.rejection_reasons),
                            "requested_placement": metrics.requested_region,
                            "actual_placement": metrics.actual_region,
                            "remaining_lifetime_seconds": metrics.remaining_lifetime_seconds,
                            "idle_resource_seconds": cost.get("idle_resource_seconds"),
                            "estimated_idle_cost_usd": _nested_number(
                                cost,
                                "estimated_cost",
                                "total",
                            ),
                        }
                    )
                except Exception as exc:
                    status = "timeout" if isinstance(exc, TimeoutError) else "failed"
                    attempt = _attempt_row(
                        index,
                        status=status,
                        failure={"phase": "warm_claim", "error_type": type(exc).__name__},
                    )
                    attempt.update(
                        {
                            "pool_hit": False,
                            "pool_miss": False,
                            "cold_fallback": False,
                            "rejection_reasons": [],
                            "requested_placement": config.region,
                            "actual_placement": None,
                            "idle_resource_seconds": None,
                            "estimated_idle_cost_usd": None,
                        }
                    )
                attempt["cleanup"] = _close_claim(claim)
                attempts.append(attempt)
                if progress is not None:
                    progress("warm_claim", len(attempts), config.warm_claim_attempts)
    finally:
        cleanup = _cleanup_pool(pool_manager, pool_name=pool_name)
    return attempts, {
        "pool_name_hash": hashlib.sha256(pool_name.encode()).hexdigest()[:16],
        "pool_target_size": config.warm_pool_target,
        "idle_hold_seconds_per_batch": config.warm_idle_seconds,
        "fill_failures": fill_failures,
        "final_cleanup": cleanup,
    }


def modal_optimization_computer_config(
    config: ModalOptimizationConfig,
    *,
    run_id: str,
) -> ComputerConfig:
    return ComputerConfig(
        runtime=RuntimeConfig(
            timeout_seconds=config.sandbox_timeout_seconds,
            readiness_timeout_seconds=int(config.readiness_timeout_seconds),
            modal_region=config.region,
        ),
        resources=ResourceConfig(
            profile="browser",
            cpu=config.cpu,
            memory_mib=config.memory_mib,
        ),
        image=ImageConfig(source="named", revision=config.image_revision),
        browser=BrowserConfig(kind=config.browser, prewarm=True),
        actions=ActionConfig(input_rate_limit_per_sec=0),
        run_id=run_id,
        ingress=config.ingress,
    )


def _runner_action_case(result: dict[str, Any]) -> dict[str, Any]:
    surfaces = _mapping(result.get("surfaces"), "runner surfaces")
    observation = _mapping(
        surfaces.get("daemon-observation-stream"),
        "daemon-observation-stream",
    )
    cases = _mapping(observation.get("cases"), "observation cases")
    return _mapping(cases.get(OPTIMIZED_ACTION_CASE), OPTIMIZED_ACTION_CASE)


def _cleanup_computer(computer: Any | None) -> dict[str, Any]:
    if computer is None:
        return {
            "attempted": False,
            "succeeded": None,
            "error_type": None,
        }
    error_type: str | None = None
    try:
        computer.terminate(wait=True)
    except Exception as exc:
        error_type = type(exc).__name__
    try:
        computer.detach()
    except Exception as exc:
        error_type = error_type or type(exc).__name__
    return {
        "attempted": True,
        "succeeded": error_type is None,
        "error_type": error_type,
    }


def _close_claim(claim: Any | None) -> dict[str, Any]:
    if claim is None:
        return {
            "attempted": False,
            "succeeded": None,
            "error_type": None,
        }
    try:
        claim.close()
    except Exception as exc:
        return {
            "attempted": True,
            "succeeded": False,
            "error_type": type(exc).__name__,
        }
    return {"attempted": True, "succeeded": True, "error_type": None}


def _cleanup_pool(manager: ComputerSandboxManager, *, pool_name: str) -> dict[str, Any]:
    inspected = 0
    terminated = 0
    failures: list[str] = []
    try:
        entries = manager.registry.list_sandboxes_with_refs(
            tags={"computer-use.pool": pool_name}
        )
    except Exception as exc:
        return {
            "inspected": 0,
            "terminated": 0,
            "failure_types": [type(exc).__name__],
        }
    for sandbox, _ref in entries:
        inspected += 1
        terminate = getattr(sandbox, "terminate", None)
        if not callable(terminate):
            failures.append("TerminateUnavailable")
            continue
        try:
            terminate(wait=True)
        except Exception as exc:
            failures.append(type(exc).__name__)
        else:
            terminated += 1
    return {
        "inspected": inspected,
        "terminated": terminated,
        "failure_types": failures,
    }

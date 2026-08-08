"""Measure the prior two-request path and Computer Step in one placed borrow."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from ..models import ActionBatchResult, Screenshot, ScreenshotOptions
from .step_promotion_gate import (
    CANDIDATE_ARM,
    MINIMUM_SAMPLES_PER_ARM,
    PRIOR_PUBLIC_ARM,
    build_step_interleaved_schedule,
    validate_step_promotion_artifact,
)

_SCREENSHOT_OPTIONS = {
    "format": "png",
    "quality": 90,
    "scale": 1.0,
    "show_cursor": False,
    "processing": "daemon",
    "storage": "inline",
}
_LIFECYCLE_TIMINGS = ("cold_start_ms", "startup_ms", "dispatch_ms")


class _StepComputer(Protocol):
    actions: Any
    screenshots: Any

    async def step(self, actions: list[dict[str, Any]], **kwargs: Any) -> Any: ...


class _MeasurementFailure(RuntimeError):
    def __init__(self, category: str) -> None:
        super().__init__("step promotion operation failed")
        self.category = category


async def measure_interleaved_step_promotion(
    borrow: Callable[[], AbstractAsyncContextManager[_StepComputer]],
    *,
    actions: Sequence[Mapping[str, Any]],
    prepare: Callable[[_StepComputer, int, str], object | Awaitable[object]],
    verify_frame: Callable[[Screenshot, object], bool],
    configuration: Mapping[str, Any],
    lifecycle_timings: Mapping[str, float],
    sample_count: int = MINIMUM_SAMPLES_PER_ARM,
    warmup_iterations: int = 2,
    schedule_seed: int = 20260808,
    bootstrap_seed: int = 20260808,
    bootstrap_resamples: int = 2_000,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, dict[str, Any]]:
    """Return sanitized paired artifacts from one borrowed trajectory."""

    if isinstance(sample_count, bool) or sample_count < MINIMUM_SAMPLES_PER_ARM:
        raise ValueError(f"sample_count must be at least {MINIMUM_SAMPLES_PER_ARM}")
    if isinstance(warmup_iterations, bool) or warmup_iterations < 0:
        raise ValueError("warmup_iterations must be nonnegative")
    if isinstance(bootstrap_resamples, bool) or bootstrap_resamples < 100:
        raise ValueError("bootstrap_resamples must be at least 100")
    action_payload = [dict(action) for action in actions]
    if not action_payload:
        raise ValueError("actions must contain one ordered batch")
    for name in _LIFECYCLE_TIMINGS:
        _nonnegative_timing(lifecycle_timings.get(name), name)

    schedule = build_step_interleaved_schedule(
        samples_per_arm=sample_count,
        warmup_iterations=warmup_iterations,
        seed=schedule_seed,
    )
    observations: dict[str, list[dict[str, Any]]] = {
        arm: [] for arm in (PRIOR_PUBLIC_ARM, CANDIDATE_ARM)
    }
    failures: dict[str, list[dict[str, Any]]] = {
        arm: [] for arm in (PRIOR_PUBLIC_ARM, CANDIDATE_ARM)
    }
    borrow_started = clock()
    borrow_ms = 0.0
    cleanup_succeeded = False
    stopped = False
    borrow_entered = False

    try:
        async with borrow() as computer:
            borrow_entered = True
            borrow_ms = max(0.0, (clock() - borrow_started) * 1000.0)
            for row in schedule:
                arm = str(row["arm"])
                pair_index = int(row["pair_index"])
                sample_index = row["sample_index"]
                try:
                    preparation = prepare(computer, pair_index, arm)
                    token = (
                        await preparation
                        if inspect.isawaitable(preparation)
                        else preparation
                    )
                    observation = await _measure_operation(
                        computer,
                        arm=arm,
                        actions=action_payload,
                        token=token,
                        verify_frame=verify_frame,
                        sample_index=int(sample_index) if sample_index is not None else None,
                        lifecycle_timings=lifecycle_timings,
                        borrow_ms=borrow_ms,
                        configuration=configuration,
                        clock=clock,
                    )
                except Exception as exc:
                    phase = "warmup" if row["phase"] == "warmup" else "measure"
                    failures[arm].append(
                        {
                            "phase": phase,
                            "sample_index": sample_index,
                            "status": "failed",
                            "error_category": _error_category(exc),
                        }
                    )
                    if row["phase"] == "measure":
                        observations[arm].append(
                            _failed_observation(
                                sample_index=int(sample_index),
                                lifecycle_timings=lifecycle_timings,
                                borrow_ms=borrow_ms,
                                arm=arm,
                            )
                        )
                    stopped = True
                    break
                if row["phase"] == "measure":
                    observations[arm].append(observation)
    except Exception as exc:
        phase = "cleanup" if borrow_entered else "borrow"
        category = "cleanup" if borrow_entered else _error_category(exc)
        for arm in (PRIOR_PUBLIC_ARM, CANDIDATE_ARM):
            failures[arm].append(
                {
                    "phase": phase,
                    "sample_index": None,
                    "status": "failed",
                    "error_category": category,
                }
            )
        stopped = True
    else:
        cleanup_succeeded = True

    artifacts = {
        arm: _build_artifact(
            arm=arm,
            configuration=configuration,
            actions=action_payload,
            schedule=schedule,
            observations=observations[arm],
            failures=failures[arm],
            cleanup_succeeded=cleanup_succeeded,
            stopped=stopped,
            sample_count=sample_count,
            warmup_iterations=warmup_iterations,
            schedule_seed=schedule_seed,
            bootstrap_seed=bootstrap_seed,
            bootstrap_resamples=bootstrap_resamples,
        )
        for arm in (PRIOR_PUBLIC_ARM, CANDIDATE_ARM)
    }
    for artifact in artifacts.values():
        if artifact["status"] == "complete":
            validate_step_promotion_artifact(artifact, expected_arm=str(artifact["arm"]))
    return artifacts


async def _measure_operation(
    computer: _StepComputer,
    *,
    arm: str,
    actions: list[dict[str, Any]],
    token: object,
    verify_frame: Callable[[Screenshot, object], bool],
    sample_index: int | None,
    lifecycle_timings: Mapping[str, float],
    borrow_ms: float,
    configuration: Mapping[str, Any],
    clock: Callable[[], float],
) -> dict[str, Any]:
    started = clock()
    if arm == PRIOR_PUBLIC_ARM:
        action_started = clock()
        result = await computer.actions.run(
            actions,
            continue_on_error=False,
            screenshot_after=False,
            source="step-promotion-benchmark",
        )
        action_phase_ms = max(0.0, (clock() - action_started) * 1000.0)
        if not _action_succeeded(result):
            raise _MeasurementFailure("action")
        screenshot_started = clock()
        screenshot = await computer.screenshots.full(**_SCREENSHOT_OPTIONS)
        screenshot_phase_ms = max(0.0, (clock() - screenshot_started) * 1000.0)
        daemon_total_ms = None
    elif arm == CANDIDATE_ARM:
        step_result = await computer.step(
            actions,
            continue_on_error=False,
            screenshot_options=ScreenshotOptions(**_SCREENSHOT_OPTIONS),
        )
        result = step_result.actions
        screenshot = step_result.screenshot
        if not _action_succeeded(result):
            raise _MeasurementFailure("action")
        timing = step_result.timing
        action_phase_ms = _required_step_timing(timing.action_ms)
        screenshot_phase_ms = _required_step_timing(timing.screenshot_ms)
        daemon_total_ms = _required_step_timing(timing.total_ms)
    else:
        raise _MeasurementFailure("configuration")
    action_to_frame_ms = max(0.0, (clock() - started) * 1000.0)
    try:
        frame_valid = bool(screenshot.as_bytes())
    except Exception as exc:
        raise _MeasurementFailure("frame") from exc
    if not frame_valid:
        raise _MeasurementFailure("frame")
    if not verify_frame(screenshot, token):
        raise _MeasurementFailure("freshness")
    if _observed_input_backend(result) != configuration.get("input_backend"):
        raise _MeasurementFailure("attribution")
    transport_and_decode_ms = (
        max(0.0, action_to_frame_ms - daemon_total_ms)
        if daemon_total_ms is not None
        else None
    )
    return {
        "sample_index": sample_index,
        "status": "ok",
        "frame_valid": True,
        "freshness_verified": True,
        "cursor_position_verified": True,
        "capture_after_baseline": True,
        "connection_reused": configuration.get("connection_reuse")
        == "one-pooled-async-client",
        "borrow_count": 1,
        "timings_ms": {
            "cold_start_ms": float(lifecycle_timings["cold_start_ms"]),
            "startup_ms": float(lifecycle_timings["startup_ms"]),
            "dispatch_ms": float(lifecycle_timings["dispatch_ms"]),
            "borrow_ms": borrow_ms,
            "action_to_frame_ms": action_to_frame_ms,
            "action_phase_ms": action_phase_ms,
            "screenshot_phase_ms": screenshot_phase_ms,
            "daemon_total_ms": daemon_total_ms,
            "transport_and_decode_ms": transport_and_decode_ms,
        },
        "attribution": {
            "input_backend": "xtest",
            "screenshot_transport": "raw-binary",
            "operation_transport": _operation_transport(arm),
        },
        "cleanup": {"attempted": False, "succeeded": False, "survivors": 1},
    }


def _build_artifact(
    *,
    arm: str,
    configuration: Mapping[str, Any],
    actions: list[dict[str, Any]],
    schedule: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    cleanup_succeeded: bool,
    stopped: bool,
    sample_count: int,
    warmup_iterations: int,
    schedule_seed: int,
    bootstrap_seed: int,
    bootstrap_resamples: int,
) -> dict[str, Any]:
    cleanup = {
        "attempted": True,
        "succeeded": cleanup_succeeded,
        "survivors": 0 if cleanup_succeeded else 1,
    }
    for observation in observations:
        observation["cleanup"] = dict(cleanup)
    complete = (
        cleanup_succeeded
        and not stopped
        and not failures
        and len(observations) == sample_count
        and all(item["status"] == "ok" for item in observations)
    )
    resolved = copy.deepcopy(dict(configuration))
    resolved["action_payload_sha256"] = hashlib.sha256(
        json.dumps(actions, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    resolved["operation_transport"] = _operation_transport(arm)
    return {
        "schema_version": 1,
        "benchmark": "computer-step-promotion",
        "arm": arm,
        "status": "complete" if complete else "failed",
        "configuration": resolved,
        "preregistration": {
            "samples_per_arm": sample_count,
            "minimum_samples_per_arm": MINIMUM_SAMPLES_PER_ARM,
            "warmup_iterations": warmup_iterations,
            "schedule_seed": schedule_seed,
            "bootstrap_seed": bootstrap_seed,
            "bootstrap_resamples": bootstrap_resamples,
        },
        "schedule": schedule,
        "observations": observations,
        "failures": failures,
        "cleanup": cleanup,
        "replacement_samples": 0,
        "retries": 0,
    }


def _failed_observation(
    *,
    sample_index: int,
    lifecycle_timings: Mapping[str, float],
    borrow_ms: float,
    arm: str,
) -> dict[str, Any]:
    return {
        "sample_index": sample_index,
        "status": "failed",
        "frame_valid": False,
        "freshness_verified": False,
        "cursor_position_verified": False,
        "capture_after_baseline": False,
        "connection_reused": True,
        "borrow_count": 1,
        "timings_ms": {
            "cold_start_ms": float(lifecycle_timings["cold_start_ms"]),
            "startup_ms": float(lifecycle_timings["startup_ms"]),
            "dispatch_ms": float(lifecycle_timings["dispatch_ms"]),
            "borrow_ms": borrow_ms,
            "action_to_frame_ms": 0.0,
            "action_phase_ms": 0.0,
            "screenshot_phase_ms": 0.0,
            "daemon_total_ms": 0.0,
            "transport_and_decode_ms": 0.0,
        },
        "attribution": {
            "input_backend": "xtest",
            "screenshot_transport": "raw-binary",
            "operation_transport": _operation_transport(arm),
        },
        "cleanup": {"attempted": False, "succeeded": False, "survivors": 1},
    }


def _operation_transport(arm: str) -> str:
    return (
        "actions-run-then-screenshots-full"
        if arm == PRIOR_PUBLIC_ARM
        else "computer-step-envelope-v1"
    )


def _action_succeeded(result: Any) -> bool:
    return isinstance(result, ActionBatchResult) and result.ok and all(
        item.ok for item in result.results
    )


def _observed_input_backend(result: ActionBatchResult) -> str | None:
    backends = {
        item.output.get("input_backend")
        for item in result.results
        if isinstance(item.output.get("input_backend"), str)
    }
    return backends.pop() if len(backends) == 1 else None


def _error_category(exc: BaseException) -> str:
    if isinstance(exc, _MeasurementFailure):
        return exc.category
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "operation"


def _required_step_timing(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise _MeasurementFailure("attribution")
    result = float(value)
    if not math.isfinite(result):
        raise _MeasurementFailure("attribution")
    return result


def _nonnegative_timing(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return float(value)

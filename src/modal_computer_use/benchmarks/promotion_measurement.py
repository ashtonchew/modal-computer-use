"""Measure the optimized default and prior screenshot path in one placed trajectory.

The application-owned Modal Function supplies the borrow context. This module stays
provider-neutral and Modal-free. It enters that context once, follows the preregistered
interleaved schedule, and uses one ordered HTTP action batch for every operation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from ..errors import DaemonHTTPError
from ..models import ActionBatchResult, Screenshot
from .promotion_gate import (
    CANDIDATE_ARM,
    MINIMUM_SAMPLES_PER_ARM,
    PRIOR_PUBLIC_ARM,
    build_interleaved_schedule,
    validate_promotion_artifact,
)

_TIMING_NAMES = ("cold_start_ms", "startup_ms", "dispatch_ms")
_SCREENSHOT_PAYLOAD: dict[str, Any] = {
    "format": "png",
    "quality": 90,
    "scale": 1.0,
    "show_cursor": False,
    "processing": "daemon",
    "storage": "inline",
}
_ACTION_SOURCE = "promotion-benchmark"


class _PromotionMeasurementFailure(RuntimeError):
    """Carry one fixed, secret-free failure category across the measurement seam."""

    def __init__(self, category: str) -> None:
        super().__init__("promotion operation failed")
        self.category = category


class _PromotionComputer(Protocol):
    client: Any
    screenshots: Any
    actions: Any


class _PromotionAdapter(Protocol):
    arm: str
    screenshot_transport: str
    http2: bool

    async def screenshot(self, computer: _PromotionComputer) -> Screenshot: ...


class CandidateDefaultAdapter:
    """Measure the candidate through the public semantic screenshot Interface."""

    arm = CANDIDATE_ARM
    screenshot_transport = "raw-binary"
    http2 = False

    async def screenshot(self, computer: _PromotionComputer) -> Screenshot:
        screenshot = await computer.screenshots.full(**_SCREENSHOT_PAYLOAD)
        if not isinstance(screenshot, Screenshot):
            raise TypeError("candidate screenshot did not return a Screenshot")
        if screenshot.bytes is None or screenshot.data_base64 is not None:
            raise ValueError("candidate screenshot was not byte-backed")
        return screenshot


class PriorPublicCompatibilityAdapter:
    """Measure the retained inline JSON/base64 screenshot representation."""

    arm = PRIOR_PUBLIC_ARM
    screenshot_transport = "json-base64"
    http2 = False

    async def screenshot(self, computer: _PromotionComputer) -> Screenshot:
        payload = await computer.client.post_json(
            "/v1/screenshots/full",
            json=dict(_SCREENSHOT_PAYLOAD),
        )
        screenshot = Screenshot.model_validate(payload)
        if screenshot.data_base64 is None or screenshot.bytes is not None:
            raise ValueError("prior screenshot was not JSON/base64-backed")
        return screenshot


def adapter_for_arm(arm: str) -> _PromotionAdapter:
    """Return the one screenshot Adapter allowed for an experiment arm."""

    if arm == CANDIDATE_ARM:
        return CandidateDefaultAdapter()
    if arm == PRIOR_PUBLIC_ARM:
        return PriorPublicCompatibilityAdapter()
    raise ValueError("promotion measurement arm is invalid")


async def measure_interleaved_promotion(
    borrow: Callable[[], AbstractAsyncContextManager[_PromotionComputer]],
    *,
    actions: Sequence[Mapping[str, Any]],
    configuration: Mapping[str, Any],
    lifecycle_timings: Mapping[str, float],
    sample_count: int = MINIMUM_SAMPLES_PER_ARM,
    warmup_iterations: int = 1,
    schedule_seed: int = 20260808,
    bootstrap_seed: int = 20260808,
    bootstrap_resamples: int = 2_000,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, dict[str, Any]]:
    """Return both arm artifacts from one borrowed, interleaved trajectory."""

    if isinstance(sample_count, bool) or sample_count < MINIMUM_SAMPLES_PER_ARM:
        raise ValueError(f"sample_count must be at least {MINIMUM_SAMPLES_PER_ARM}")
    if isinstance(warmup_iterations, bool) or warmup_iterations < 0:
        raise ValueError("warmup_iterations must be nonnegative")
    for name in _TIMING_NAMES:
        _require_timing(lifecycle_timings.get(name), name)
    if isinstance(bootstrap_seed, bool) or bootstrap_seed < 1:
        raise ValueError("bootstrap_seed must be positive")
    if isinstance(bootstrap_resamples, bool) or bootstrap_resamples < 100:
        raise ValueError("bootstrap_resamples must be at least 100")

    action_payload = [dict(action) for action in actions]
    if not action_payload:
        raise ValueError("actions must contain one ordered batch")
    action_digest = hashlib.sha256(
        json.dumps(action_payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    schedule = build_interleaved_schedule(
        samples_per_arm=sample_count,
        warmup_iterations=warmup_iterations,
        seed=schedule_seed,
    )
    observations: dict[str, list[dict[str, Any]]] = {
        PRIOR_PUBLIC_ARM: [],
        CANDIDATE_ARM: [],
    }
    failures: dict[str, list[dict[str, Any]]] = {
        PRIOR_PUBLIC_ARM: [],
        CANDIDATE_ARM: [],
    }
    borrow_started = clock()
    borrow_ms = 0.0
    cleanup_succeeded = False

    try:
        async with borrow() as computer:
            borrow_ms = max(0.0, (clock() - borrow_started) * 1000.0)
            for row in schedule:
                arm = str(row["arm"])
                adapter = adapter_for_arm(arm)
                sample_index = row.get("sample_index")
                if row["phase"] == "warmup":
                    try:
                        await _run_operation(adapter, computer, action_payload)
                    except Exception as exc:
                        failures[arm].append(
                            _failure_record(phase="warmup", sample_index=None, exc=exc)
                        )
                        break
                    continue
                observation = await _measure_sample(
                    adapter,
                    computer,
                    action_payload,
                    sample_index=int(sample_index),
                    lifecycle_timings=lifecycle_timings,
                    borrow_ms=borrow_ms,
                    configuration=_arm_configuration(
                        configuration,
                        arm=arm,
                        action_digest=action_digest,
                        warmup_iterations=warmup_iterations,
                    ),
                    clock=clock,
                    failures=failures[arm],
                )
                observations[arm].append(observation)
                if observation["status"] != "ok":
                    break
        cleanup_succeeded = True
    except Exception as exc:
        for arm in (PRIOR_PUBLIC_ARM, CANDIDATE_ARM):
            failures[arm].append(_failure_record(phase="borrow", sample_index=None, exc=exc))

    artifacts = {
        arm: _build_artifact(
            arm=arm,
            configuration=_arm_configuration(
                configuration,
                arm=arm,
                action_digest=action_digest,
                warmup_iterations=warmup_iterations,
            ),
            schedule=schedule,
            observations=observations[arm],
            failures=failures[arm],
            cleanup_succeeded=cleanup_succeeded,
            sample_count=sample_count,
            warmup_iterations=warmup_iterations,
            schedule_seed=schedule_seed,
            bootstrap_seed=bootstrap_seed,
            bootstrap_resamples=bootstrap_resamples,
        )
        for arm in (PRIOR_PUBLIC_ARM, CANDIDATE_ARM)
    }
    for artifact in artifacts.values():
        if len(artifact["observations"]) == sample_count:
            validate_promotion_artifact(artifact, expected_arm=str(artifact["arm"]))
    return artifacts


async def _run_operation(
    adapter: _PromotionAdapter,
    computer: _PromotionComputer,
    actions: list[dict[str, Any]],
) -> tuple[Screenshot, Any]:
    try:
        screenshot = await adapter.screenshot(computer)
    except Exception as exc:
        raise _PromotionMeasurementFailure("screenshot") from exc
    try:
        frame = screenshot.as_bytes()
    except Exception as exc:
        raise _PromotionMeasurementFailure("frame") from exc
    if not frame:
        raise _PromotionMeasurementFailure("frame")
    try:
        result = await computer.actions.run(
            actions,
            continue_on_error=False,
            screenshot_after=False,
            source=_ACTION_SOURCE,
        )
    except Exception as exc:
        raise _PromotionMeasurementFailure("action") from exc
    if not _action_succeeded(result):
        raise _PromotionMeasurementFailure("action")
    return screenshot, result


async def _measure_sample(
    adapter: _PromotionAdapter,
    computer: _PromotionComputer,
    actions: list[dict[str, Any]],
    *,
    sample_index: int,
    lifecycle_timings: Mapping[str, float],
    borrow_ms: float,
    configuration: Mapping[str, Any],
    clock: Callable[[], float],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    started = clock()
    frame_valid = False
    try:
        screenshot, action_result = await _run_operation(adapter, computer, actions)
        frame_valid = bool(screenshot.as_bytes())
        warm_operation_ms = max(0.0, (clock() - started) * 1000.0)
        observed_backend = _observed_input_backend(action_result)
        if observed_backend != configuration["input_backend"]:
            raise _PromotionMeasurementFailure("attribution")
        daemon_ms = _observed_daemon_ms(action_result, warm_operation_ms)
        status = "ok"
    except Exception as exc:
        warm_operation_ms = max(0.0, (clock() - started) * 1000.0)
        observed_backend = configuration["input_backend"]
        daemon_ms = warm_operation_ms
        status = "failed"
        failures.append(_failure_record(phase="measure", sample_index=sample_index, exc=exc))
    return {
        "sample_index": sample_index,
        "status": status,
        "frame_valid": frame_valid,
        "connection_reused": configuration.get("connection_reuse")
        == "one-pooled-async-client",
        "borrow_count": 1,
        "timings_ms": {
            "cold_start_ms": lifecycle_timings["cold_start_ms"],
            "startup_ms": lifecycle_timings["startup_ms"],
            "dispatch_ms": lifecycle_timings["dispatch_ms"],
            "borrow_ms": borrow_ms,
            "warm_operation_ms": warm_operation_ms,
        },
        "cleanup": {"attempted": False, "succeeded": False, "survivors": 1},
        "attribution": {
            "daemon_ms": daemon_ms,
            "input_backend": observed_backend,
            "screenshot_transport": adapter.screenshot_transport,
        },
    }


def _build_artifact(
    *,
    arm: str,
    configuration: dict[str, Any],
    schedule: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    cleanup_succeeded: bool,
    sample_count: int,
    warmup_iterations: int,
    schedule_seed: int,
    bootstrap_seed: int,
    bootstrap_resamples: int,
) -> dict[str, Any]:
    for observation in observations:
        observation["cleanup"] = {
            "attempted": True,
            "succeeded": cleanup_succeeded,
            "survivors": 0 if cleanup_succeeded else 1,
        }
    complete = (
        cleanup_succeeded
        and not failures
        and len(observations) == sample_count
        and all(row["status"] == "ok" for row in observations)
    )
    return {
        "schema_version": 1,
        "benchmark": "optimized-default-promotion",
        "arm": arm,
        "status": "complete" if complete else "failed",
        "configuration": configuration,
        "preregistration": {
            "samples_per_arm": sample_count,
            "warmup_iterations": warmup_iterations,
            "schedule_seed": schedule_seed,
            "bootstrap_seed": bootstrap_seed,
            "bootstrap_resamples": bootstrap_resamples,
            "minimum_samples_per_arm": MINIMUM_SAMPLES_PER_ARM,
        },
        "schedule": schedule,
        "observations": observations,
        "failures": failures,
        "cleanup": {
            "attempted": True,
            "succeeded": cleanup_succeeded,
            "survivors": 0 if cleanup_succeeded else 1,
        },
        "replacement_samples": 0,
        "retries": 0,
    }


def _arm_configuration(
    configuration: Mapping[str, Any],
    *,
    arm: str,
    action_digest: str,
    warmup_iterations: int,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(configuration))
    screenshot = dict(result.get("screenshot", {}))
    screenshot.update(
        {
            "format": "png",
            "show_cursor": False,
            "transport": adapter_for_arm(arm).screenshot_transport,
        }
    )
    result["screenshot"] = screenshot
    result["action_payload_sha256"] = action_digest
    result["warmup_iterations"] = warmup_iterations
    return result


def _action_succeeded(result: Any) -> bool:
    if isinstance(result, ActionBatchResult):
        return result.ok and all(item.ok for item in result.results)
    if isinstance(result, Mapping):
        items = result.get("results")
        return result.get("ok") is True and isinstance(items, list) and all(
            isinstance(item, Mapping) and item.get("ok") is True for item in items
        )
    return False


def _observed_input_backend(result: Any) -> str | None:
    outputs: list[Mapping[str, Any]]
    if isinstance(result, ActionBatchResult):
        outputs = [item.output for item in result.results]
    elif isinstance(result, Mapping) and isinstance(result.get("results"), list):
        outputs = [
            item.get("output", {})
            for item in result["results"]
            if isinstance(item, Mapping) and isinstance(item.get("output", {}), Mapping)
        ]
    else:
        outputs = []
    backends = {
        output.get("input_backend")
        for output in outputs
        if isinstance(output.get("input_backend"), str)
    }
    return backends.pop() if len(backends) == 1 else None


def _observed_daemon_ms(result: Any, fallback: float) -> float:
    outputs: list[Mapping[str, Any]]
    if isinstance(result, ActionBatchResult):
        outputs = [item.output for item in result.results]
    elif isinstance(result, Mapping) and isinstance(result.get("results"), list):
        outputs = [
            item.get("output", {})
            for item in result["results"]
            if isinstance(item, Mapping) and isinstance(item.get("output", {}), Mapping)
        ]
    else:
        outputs = []
    values = [
        float(value)
        for output in outputs
        if isinstance((value := output.get("daemon_ms")), (int, float))
        and not isinstance(value, bool)
        and value >= 0
    ]
    return max(values) if values else fallback


def _failure_record(*, phase: str, sample_index: int | None, exc: Exception) -> dict[str, Any]:
    record: dict[str, Any] = {
        "phase": phase,
        "sample_index": sample_index,
        "error_category": _error_category(exc),
    }
    status = _daemon_http_status(exc)
    if status is not None:
        record["http_status"] = status
    return record


def _daemon_http_status(exc: Exception) -> int | None:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, DaemonHTTPError):
            status = current.status_code
            if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
                return status
            return None
        current = current.__cause__ or current.__context__
    return None


def _error_category(exc: Exception) -> str:
    if isinstance(exc, _PromotionMeasurementFailure):
        return exc.category
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (ConnectionError, OSError)):
        return "transport"
    if isinstance(exc, (TypeError, ValueError)):
        return "validation"
    return "operation"


def _require_timing(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be non-null and nonnegative")
    return float(value)


run_async_promotion_measurement = measure_interleaved_promotion

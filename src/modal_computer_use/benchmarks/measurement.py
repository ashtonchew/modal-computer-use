from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable
from typing import Any, Protocol

from .safety import _failure


class _RecordingBenchmark(Protocol):
    def run(self) -> dict[str, Any]:
        raise NotImplementedError

    def start(self) -> Any:
        raise NotImplementedError

    def stop(self, started: Any) -> dict[str, Any]:
        raise NotImplementedError


def _measure_case(
    *,
    name: str,
    iterations: int,
    warmup_iterations: int,
    operation: Callable[[], None],
    failures: list[dict[str, Any]],
) -> list[float]:
    samples, _observations = _measure_observed_case(
        name=name,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=operation,
        failures=failures,
    )
    return samples

def _measure_recording_start_stop(
    *,
    iterations: int,
    warmup_iterations: int,
    benchmark: _RecordingBenchmark,
    failures: list[dict[str, Any]],
) -> tuple[list[float], list[float], list[dict[str, Any]]]:
    start_samples: list[float] = []
    stop_samples: list[float] = []
    observations: list[dict[str, Any]] = []
    for warmup_index in range(warmup_iterations):
        try:
            benchmark.run()
        except Exception as exc:
            failures.append(
                _failure(
                    "recording_start_stop",
                    phase="warmup",
                    iteration=warmup_index,
                    exc=exc,
                )
            )
            return start_samples, stop_samples, observations
    for iteration in range(iterations):
        start = time.perf_counter()
        try:
            started = benchmark.start()
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            failures.append(
                _failure(
                    "recording_start",
                    phase="measure",
                    iteration=iteration,
                    exc=exc,
                    elapsed_ms=elapsed_ms,
                )
            )
            continue
        start_samples.append((time.perf_counter() - start) * 1000)

        stop = time.perf_counter()
        try:
            observation = benchmark.stop(started)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - stop) * 1000
            failures.append(
                _failure(
                    "recording_stop",
                    phase="measure",
                    iteration=iteration,
                    exc=exc,
                    elapsed_ms=elapsed_ms,
                )
            )
            continue
        stop_samples.append((time.perf_counter() - stop) * 1000)
        observations.append(observation)
    return start_samples, stop_samples, observations

def _measure_observed_case(
    *,
    name: str,
    iterations: int,
    warmup_iterations: int,
    operation: Callable[[], Any],
    failures: list[dict[str, Any]],
    redacted_text: str | None = None,
) -> tuple[list[float], list[Any]]:
    samples: list[float] = []
    observations: list[Any] = []
    for warmup_index in range(warmup_iterations):
        try:
            operation()
        except Exception as exc:
            failures.append(
                _failure(
                    name,
                    phase="warmup",
                    iteration=warmup_index,
                    exc=exc,
                    redacted_text=redacted_text,
                )
            )
            return samples, observations
    for iteration in range(iterations):
        start = time.perf_counter()
        try:
            observation = operation()
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            failures.append(
                _failure(
                    name,
                    phase="measure",
                    iteration=iteration,
                    exc=exc,
                    elapsed_ms=elapsed_ms,
                    redacted_text=redacted_text,
                )
            )
            continue
        samples.append((time.perf_counter() - start) * 1000)
        observations.append(observation)
    return samples, observations

def _case_result(
    name: str,
    iterations: int,
    samples: list[float],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    case_failures = [failure for failure in failures if failure["case"] == name]
    return {
        "status": "failed" if case_failures else "ok",
        "iterations": iterations,
        "successful_iterations": len(samples),
        "samples_ms": samples,
        "summary_ms": _summary(samples),
        "failures": case_failures,
    }

def _attributed_case_result(
    name: str,
    iterations: int,
    samples: list[float],
    observations: list[Any],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    result = _case_result(name, iterations, samples, failures)
    daemon_samples: list[float] = []
    overhead_samples: list[float] = []
    for sample_ms, observation in zip(samples, observations, strict=False):
        if not isinstance(observation, dict):
            continue
        daemon_ms = observation.get("daemon_ms")
        if daemon_ms is None:
            continue
        daemon_samples.append(daemon_ms)
        overhead_samples.append(max(sample_ms - daemon_ms, 0.0))
    attribution = {
        "status": "measured" if daemon_samples else "unavailable",
        "reason": None if daemon_samples else "daemon response did not include timing.daemon_ms",
    }
    result.update(
        {
            "daemon_samples_ms": daemon_samples,
            "daemon_summary_ms": _summary(daemon_samples),
            "overhead_samples_ms": overhead_samples,
            "overhead_summary_ms": _summary(overhead_samples),
            "attribution": attribution,
        }
    )
    return result

def _summary(samples: list[float]) -> dict[str, float | None]:
    if not samples:
        return {"min": None, "p50": None, "p95": None, "mean": None, "max": None}
    ordered = sorted(samples)
    return {
        "min": min(samples),
        "p50": statistics.median(ordered),
        "p95": _percentile(ordered, 95),
        "mean": statistics.fmean(samples),
        "max": max(samples),
    }

def _percentile(ordered_samples: list[float], percentile: int) -> float:
    if len(ordered_samples) == 1:
        return ordered_samples[0]
    rank = (percentile / 100) * (len(ordered_samples) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered_samples[lower]
    weight = rank - lower
    return ordered_samples[lower] * (1 - weight) + ordered_samples[upper] * weight

def _comparison(batch_case: dict[str, Any], separate_case: dict[str, Any]) -> dict[str, Any]:
    batch_mean = batch_case["summary_ms"]["mean"]
    separate_mean = separate_case["summary_ms"]["mean"]
    if batch_mean in (None, 0) or separate_mean is None:
        return {
            "status": "not_available",
            "batch_vs_separate_speedup": None,
            "mean_delta_ms": None,
        }
    speedup = separate_mean / batch_mean
    return {
        "status": "measured",
        "batch_vs_separate_speedup": speedup,
        "mean_delta_ms": separate_mean - batch_mean,
        "batch_faster": batch_mean < separate_mean,
    }

def _named_case_comparison(
    left_name: str,
    left_case: dict[str, Any],
    right_name: str,
    right_case: dict[str, Any],
) -> dict[str, Any]:
    left_mean = left_case["summary_ms"]["mean"]
    right_mean = right_case["summary_ms"]["mean"]
    if left_mean in (None, 0) or right_mean is None:
        return {
            "status": "not_available",
            "left": left_name,
            "right": right_name,
            "mean_speedup": None,
            "mean_delta_ms": None,
        }
    return {
        "status": "measured",
        "left": left_name,
        "right": right_name,
        "mean_speedup": right_mean / left_mean,
        "mean_delta_ms": right_mean - left_mean,
        "left_faster": left_mean < right_mean,
    }

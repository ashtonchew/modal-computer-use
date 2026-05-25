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
        "sample_stability": _sample_stability(samples),
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
    transport_http_versions: list[str] = []
    input_backends: list[str] = []
    for sample_ms, observation in zip(samples, observations, strict=False):
        if not isinstance(observation, dict):
            continue
        http_version = observation.get("transport_http_version")
        if isinstance(http_version, str) and http_version:
            transport_http_versions.append(http_version)
        input_backend = observation.get("input_backend")
        if isinstance(input_backend, str) and input_backend:
            input_backends.append(input_backend)
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
            "transport_http_versions": sorted(set(transport_http_versions)),
            "input_backends": sorted(set(input_backends)),
        }
    )
    return result

def _summary(samples: list[float]) -> dict[str, float | int | list[int] | None]:
    if not samples:
        return {
            "min": None,
            "p50": None,
            "p95": None,
            "mean": None,
            "max": None,
            "trimmed_mean": None,
            "mean_without_high_outliers": None,
            "mad": None,
            "jitter_ms": None,
            "mean_p50_delta_ms": None,
            "mean_p50_delta_ratio": None,
            "high_outlier_threshold": None,
            "high_outlier_count": 0,
            "high_outlier_ratio": 0.0,
            "high_outlier_indices": [],
        }
    ordered = sorted(samples)
    median = statistics.median(ordered)
    mean = statistics.fmean(samples)
    high_outlier_threshold = _high_outlier_threshold(samples)
    high_outlier_indices = [
        index for index, sample in enumerate(samples) if sample > high_outlier_threshold
    ]
    inliers = [
        sample for index, sample in enumerate(samples) if index not in set(high_outlier_indices)
    ]
    mean_p50_delta = mean - median
    return {
        "min": min(samples),
        "p50": median,
        "p95": _percentile(ordered, 95),
        "mean": mean,
        "max": max(samples),
        "trimmed_mean": _trimmed_mean(ordered),
        "mean_without_high_outliers": statistics.fmean(inliers) if inliers else None,
        "mad": _median_absolute_deviation(samples),
        "jitter_ms": _percentile(ordered, 95) - median,
        "mean_p50_delta_ms": mean_p50_delta,
        "mean_p50_delta_ratio": mean_p50_delta / median if median else None,
        "high_outlier_threshold": high_outlier_threshold,
        "high_outlier_count": len(high_outlier_indices),
        "high_outlier_ratio": len(high_outlier_indices) / len(samples),
        "high_outlier_indices": high_outlier_indices,
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

def _trimmed_mean(ordered_samples: list[float]) -> float:
    if len(ordered_samples) < 10:
        return statistics.fmean(ordered_samples)
    trim_count = math.floor(len(ordered_samples) * 0.1)
    trimmed = ordered_samples[trim_count : len(ordered_samples) - trim_count]
    return statistics.fmean(trimmed)

def _median_absolute_deviation(samples: list[float]) -> float:
    median = statistics.median(samples)
    return statistics.median([abs(sample - median) for sample in samples])

def _high_outlier_threshold(samples: list[float]) -> float:
    median = statistics.median(samples)
    scaled_mad = _median_absolute_deviation(samples) * 1.4826
    tolerance = max(scaled_mad * 3.0, abs(median) * 0.25, 1.0)
    return median + tolerance

def _sample_stability(samples: list[float]) -> dict[str, Any]:
    summary = _summary(samples)
    if not samples:
        return {
            "status": "no_samples",
            "reason": "case did not record measured samples",
        }
    mean = summary["mean"]
    inlier_mean = summary["mean_without_high_outliers"]
    high_outlier_count = summary["high_outlier_count"]
    if (
        isinstance(mean, int | float)
        and isinstance(inlier_mean, int | float)
        and isinstance(high_outlier_count, int)
        and high_outlier_count > 0
    ):
        denominator = inlier_mean or mean
        sensitivity = (mean - inlier_mean) / denominator if denominator else 0.0
        if sensitivity >= 0.05:
            return {
                "status": "outlier_sensitive",
                "reason": "mean changes by at least 5% after removing high outliers",
                "mean_without_high_outliers": inlier_mean,
                "mean_delta_ratio": sensitivity,
                "high_outlier_count": high_outlier_count,
                "high_outlier_indices": summary["high_outlier_indices"],
            }
    return {
        "status": "stable",
        "reason": "mean is not materially changed by high-outlier filtering",
        "high_outlier_count": high_outlier_count,
    }

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

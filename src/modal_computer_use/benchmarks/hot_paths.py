from __future__ import annotations

from typing import Any

from ..client import DaemonClient
from .constants import (
    COMMAND_ECHO_COMMAND,
    MOVE_CLICK_ACTIONS,
    MOVE_CLICK_SEQUENCE_ACTIONS,
    TYPE_1000_CHARS_TEXT,
    TYPE_1000_CHARS_TIMEOUT_MS,
    TYPING_BENCHMARK_METHOD,
    TYPING_BENCHMARK_TEXT,
)
from .measurement import (
    _attributed_case_result,
    _case_result,
    _measure_observed_case,
    _measure_recording_start_stop,
    _summary,
)
from .operations import (
    _CommandEchoBenchmark,
    _MoveClickBenchmark,
    _MoveClickSequenceBenchmark,
    _RecordingStartStopBenchmark,
    _ScreenshotBenchmark,
    _TypeCharsBenchmark,
)
from .safety import _safe_action_metadata, _safe_screenshot_request


def run_screenshot_benchmark(
    *,
    client: DaemonClient,
    name: str,
    request: dict[str, Any],
    iterations: int,
    warmup_iterations: int = 1,
    raw: bool = False,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    failures: list[dict[str, Any]] = []
    benchmark = _ScreenshotBenchmark(client, request, raw=raw)
    samples, observations = _measure_observed_case(
        name=name,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=benchmark.run,
        failures=failures,
    )
    result = _case_result(name, iterations, samples, failures)
    result.update(
        {
            "request": _safe_screenshot_request(request),
            "transport_encoding": "binary" if raw else "json_base64",
            "samples_bytes": [
                item["size_bytes"] for item in observations if item.get("size_bytes") is not None
            ],
            "summary_bytes": _summary(
                [
                    float(item["size_bytes"])
                    for item in observations
                    if item.get("size_bytes") is not None
                ]
            ),
            "last_result": observations[-1] if observations else None,
        }
    )
    daemon_samples = [
        float(item["daemon_ms"])
        for item in observations
        if item.get("daemon_ms") is not None
    ]
    if daemon_samples:
        result["daemon_samples_ms"] = daemon_samples
        result["daemon_summary_ms"] = _summary(daemon_samples)
        result["overhead_samples_ms"] = [
            sample - daemon_sample
            for sample, daemon_sample in zip(samples, daemon_samples, strict=False)
        ]
        result["overhead_summary_ms"] = _summary(result["overhead_samples_ms"])
    return result

def run_move_click_benchmark(
    *,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int = 1,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    failures: list[dict[str, Any]] = []
    benchmark = _MoveClickBenchmark(client)
    samples, observations = _measure_observed_case(
        name="move_click",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=benchmark.run,
        failures=failures,
    )
    result = _attributed_case_result("move_click", iterations, samples, observations, failures)
    result.update(
        {
            "action_count": len(MOVE_CLICK_ACTIONS),
            "actions": [_safe_action_metadata(action) for action in MOVE_CLICK_ACTIONS],
        }
    )
    return result

def run_move_click_sequence_benchmark(
    *,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int = 1,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    failures: list[dict[str, Any]] = []
    benchmark = _MoveClickSequenceBenchmark(client)
    samples, observations = _measure_observed_case(
        name="move_click_sequence",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=benchmark.run,
        failures=failures,
    )
    result = _attributed_case_result(
        "move_click_sequence", iterations, samples, observations, failures
    )
    result.update(
        {
            "action_count": len(MOVE_CLICK_SEQUENCE_ACTIONS),
            "actions": [_safe_action_metadata(action) for action in MOVE_CLICK_SEQUENCE_ACTIONS],
        }
    )
    return result

def run_type_100_chars_benchmark(
    *,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int = 1,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    failures: list[dict[str, Any]] = []
    benchmark = _TypeCharsBenchmark(
        client,
        TYPING_BENCHMARK_TEXT,
        method=TYPING_BENCHMARK_METHOD,
    )
    samples, observations = _measure_observed_case(
        name="type_100_chars",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=benchmark.run,
        failures=failures,
        redacted_text=TYPING_BENCHMARK_TEXT,
    )
    result = _attributed_case_result("type_100_chars", iterations, samples, observations, failures)
    result.update(
        {
            "action_count": 1,
            "request": {
                "character_count": len(TYPING_BENCHMARK_TEXT),
                "method": TYPING_BENCHMARK_METHOD,
            },
        }
    )
    return result

def run_type_1000_chars_benchmark(
    *,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int = 1,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    failures: list[dict[str, Any]] = []
    benchmark = _TypeCharsBenchmark(
        client,
        TYPE_1000_CHARS_TEXT,
        method=TYPING_BENCHMARK_METHOD,
        timeout_ms=TYPE_1000_CHARS_TIMEOUT_MS,
    )
    samples, observations = _measure_observed_case(
        name="type_1000_chars",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=benchmark.run,
        failures=failures,
        redacted_text=TYPE_1000_CHARS_TEXT,
    )
    result = _attributed_case_result("type_1000_chars", iterations, samples, observations, failures)
    result.update(
        {
            "action_count": 1,
            "request": {
                "character_count": len(TYPE_1000_CHARS_TEXT),
                "method": TYPING_BENCHMARK_METHOD,
                "timeout_ms": TYPE_1000_CHARS_TIMEOUT_MS,
            },
        }
    )
    return result

def run_command_echo_benchmark(
    *,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int = 1,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    failures: list[dict[str, Any]] = []
    benchmark = _CommandEchoBenchmark(client)
    samples, observations = _measure_observed_case(
        name="command_echo",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=benchmark.run,
        failures=failures,
    )
    result = _case_result("command_echo", iterations, samples, failures)
    result.update(
        {
            "command": {"argv": list(COMMAND_ECHO_COMMAND), "timeout_seconds": 30},
            "last_result": observations[-1] if observations else None,
        }
    )
    return result

def run_recording_start_stop_benchmark(
    *,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int = 1,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    failures: list[dict[str, Any]] = []
    benchmark = _RecordingStartStopBenchmark(client)
    start_samples, stop_samples, observations = _measure_recording_start_stop(
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        benchmark=benchmark,
        failures=failures,
    )
    return {
        "status": "failed" if failures else "ok",
        "iterations": iterations,
        "successful_iterations": len(observations),
        "start_samples_ms": start_samples,
        "stop_samples_ms": stop_samples,
        "start_summary_ms": _summary(start_samples),
        "stop_summary_ms": _summary(stop_samples),
        "request": {"format": "mp4", "fps": 5},
        "last_result": observations[-1] if observations else None,
        "failures": failures,
    }

def run_browser_render_metrics_benchmark(
    *,
    client: DaemonClient,
    url: str,
    iterations: int,
    warmup_iterations: int = 1,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    samples, observations = _measure_observed_case(
        name="browser_render_metrics",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=lambda: client.post_json(
            "/v1/browser/render-metrics",
            json={"url": url, "timeout_seconds": timeout_seconds},
        ),
        failures=failures,
    )
    successful = observations
    for iteration, result in enumerate(successful):
        if isinstance(result, dict) and result.get("ok") is False:
            failures.append(
                {
                    "case": "browser_render_metrics",
                    "phase": "measure",
                    "iteration": iteration,
                    "error": result.get("message") or "browser render metrics failed",
                    "type": "BrowserRenderMetricsError",
                }
            )
    return {
        "status": "failed" if failures else "ok",
        "url": url,
        "iterations": iterations,
        "samples_ms": samples,
        "summary_ms": _summary(samples),
        "successful_iterations": len(samples),
        "last_result": _browser_render_last_result(successful[-1] if successful else None),
        "failures": failures,
    }

def _browser_render_last_result(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    metrics = result.get("metrics")
    navigation = metrics.get("navigation") if isinstance(metrics, dict) else None
    return {
        "ok": result.get("ok"),
        "wall_ms": result.get("wall_ms"),
        "gpu_mode": result.get("gpu_mode"),
        "profile_dir": result.get("profile_dir"),
        "metrics": {
            "url": metrics.get("url") if isinstance(metrics, dict) else None,
            "readyState": metrics.get("readyState") if isinstance(metrics, dict) else None,
            "title": metrics.get("title") if isinstance(metrics, dict) else None,
            "bodyTextLength": metrics.get("bodyTextLength") if isinstance(metrics, dict) else None,
            "webgl": metrics.get("webgl") if isinstance(metrics, dict) else None,
            "navigation": {
                "duration": navigation.get("duration") if isinstance(navigation, dict) else None,
                "domContentLoadedEventEnd": navigation.get("domContentLoadedEventEnd")
                if isinstance(navigation, dict)
                else None,
                "loadEventEnd": navigation.get("loadEventEnd")
                if isinstance(navigation, dict)
                else None,
                "responseStart": navigation.get("responseStart")
                if isinstance(navigation, dict)
                else None,
                "transferSize": navigation.get("transferSize")
                if isinstance(navigation, dict)
                else None,
                "decodedBodySize": navigation.get("decodedBodySize")
                if isinstance(navigation, dict)
                else None,
            },
            "paint": metrics.get("paint") if isinstance(metrics, dict) else None,
        },
    }

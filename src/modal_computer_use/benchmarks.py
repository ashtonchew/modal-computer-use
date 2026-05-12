from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

import httpx
from fastapi.testclient import TestClient

from ._version import __version__
from .client import DaemonClient
from .daemon.app import create_app
from .daemon.settings import DaemonSettings
from .errors import DaemonHTTPError
from .transports.http import HTTPTransport

BenchmarkMode = Literal["mock-local", "http"]
FutureBenchmarkStatus = Literal["not_measured", "unsupported"]

ACTION_BATCH_ACTIONS: list[dict[str, Any]] = [
    {"type": "move", "x": 10, "y": 10},
    {"type": "cursor_position"},
    {"type": "wait", "duration_ms": 0},
    {"type": "move", "x": 20, "y": 20},
    {"type": "cursor_position"},
]


def run_benchmark_report(
    *,
    client: DaemonClient,
    mode: BenchmarkMode,
    iterations: int,
    base_url: str | None = None,
    warmup_iterations: int = 1,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    failures: list[dict[str, Any]] = []
    metadata = _collect_metadata(client, failures)
    action_batch = run_action_batch_benchmark(
        client=client,
        mode=mode,
        iterations=iterations,
        base_url=base_url,
        warmup_iterations=warmup_iterations,
    )
    screenshot_full = run_screenshot_benchmark(
        client=client,
        name="screenshot_full",
        request={"format": "png", "storage": "inline", "show_cursor": False},
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    screenshot_compressed = run_screenshot_benchmark(
        client=client,
        name="screenshot_compressed",
        request={
            "format": "jpeg",
            "quality": 60,
            "scale": 0.5,
            "storage": "inline",
            "show_cursor": False,
        },
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    benchmarks = {
        "action_batch": _report_action_batch(action_batch),
        "screenshot_full": screenshot_full,
        "screenshot_compressed": screenshot_compressed,
        "move_click": _future_benchmark(
            "not_measured",
            "move+click benchmark will be added after the report surface is stable",
        ),
        "type_100_chars": _future_benchmark(
            "not_measured",
            "typing benchmark is deferred to avoid adding typed text payloads to this pass",
        ),
        "recording_start_stop": _future_benchmark(
            "not_measured",
            "recording benchmark is deferred until live file-size validation is covered",
        ),
        "sandbox_exec": _future_benchmark(
            "not_measured",
            "Sandbox.exec comparison requires an explicit Modal/live mode and is not run here",
        ),
        "cold_create_to_ready": _future_benchmark(
            "not_measured",
            "cold Modal Sandbox creation is outside mock-local and live-daemon benchmark modes",
        ),
        "warm_attach_to_health": _future_benchmark(
            "not_measured",
            "warm attach requires Modal orchestration and is outside this report mode",
        ),
    }
    failures.extend(_benchmark_failures("action_batch", action_batch.get("failures", [])))
    failures.extend(_benchmark_failures("screenshot_full", screenshot_full.get("failures", [])))
    failures.extend(
        _benchmark_failures("screenshot_compressed", screenshot_compressed.get("failures", []))
    )
    ok = not failures
    return {
        "ok": ok,
        "generated_at": datetime.now(UTC).isoformat(),
        "package_version": __version__,
        "mode": mode,
        "base_url": base_url,
        "iterations": iterations,
        "warmup_iterations": warmup_iterations,
        "metadata": metadata,
        "benchmarks": benchmarks,
        "failures": failures,
    }


def run_benchmark_report_mock_local(*, iterations: int) -> dict[str, Any]:
    return _with_mock_local_client(
        lambda client: run_benchmark_report(
            client=client,
            mode="mock-local",
            iterations=iterations,
            base_url="http://testserver",
        )
    )


def run_action_batch_benchmark(
    *,
    client: DaemonClient,
    mode: BenchmarkMode,
    iterations: int,
    base_url: str | None = None,
    warmup_iterations: int = 1,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    benchmark = _ActionBatchBenchmark(client)
    failures: list[dict[str, Any]] = []
    batch_samples = _measure_case(
        name="batch_5_actions",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=benchmark.run_batch,
        failures=failures,
    )
    separate_samples = _measure_case(
        name="separate_5_actions",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=benchmark.run_separate,
        failures=failures,
    )
    batch_case = _case_result("batch_5_actions", iterations, batch_samples, failures)
    separate_case = _case_result("separate_5_actions", iterations, separate_samples, failures)
    comparison = _comparison(batch_case, separate_case)
    ok = not failures
    return {
        "ok": ok,
        "benchmark": "action-batch",
        "timestamp": datetime.now(UTC).isoformat(),
        "package_version": __version__,
        "mode": mode,
        "base_url": base_url,
        "iterations": iterations,
        "warmup_iterations": warmup_iterations,
        "action_count": len(ACTION_BATCH_ACTIONS),
        "actions": [{"type": action["type"]} for action in ACTION_BATCH_ACTIONS],
        "cases": {
            "batch_5_actions": batch_case,
            "separate_5_actions": separate_case,
            "sandbox_exec": {
                "status": "not_measured",
                "reason": "Sandbox.exec comparison is unsupported in this benchmark pass",
            },
        },
        "comparison": comparison,
        "failures": failures,
    }


def run_action_batch_benchmark_mock_local(*, iterations: int) -> dict[str, Any]:
    return _with_mock_local_client(
        lambda client: run_action_batch_benchmark(
            client=client,
            mode="mock-local",
            iterations=iterations,
            base_url="http://testserver",
        )
    )


def run_screenshot_benchmark(
    *,
    client: DaemonClient,
    name: str,
    request: dict[str, Any],
    iterations: int,
    warmup_iterations: int = 1,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    failures: list[dict[str, Any]] = []
    benchmark = _ScreenshotBenchmark(client, request)
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
    return result


def _with_mock_local_client(callback: Callable[[DaemonClient], dict[str, Any]]) -> dict[str, Any]:
    with TemporaryDirectory(prefix="modal-computer-use-benchmark-") as temp_dir:
        root = Path(temp_dir)
        with redirect_stdout(StringIO()):
            app = create_app(
                DaemonSettings(
                    backend="mock",
                    artifacts_dir=root / "artifacts",
                    recordings_dir=root / "recordings",
                    trace_dir=root / "artifacts" / "traces",
                    local_token="dev",  # noqa: S106 - mock-local benchmark auth only.
                )
            )
            with TestClient(app, headers={"Authorization": "Bearer dev"}) as test_client:
                transport = HTTPTransport(
                    "http://testserver",
                    token="dev",  # noqa: S106 - mock-local benchmark auth only.
                    client=test_client,
                )
                client = DaemonClient(
                    "http://testserver",
                    token="dev",  # noqa: S106 - mock-local benchmark auth only.
                    transport=transport,
                )
                try:
                    return callback(client)
                finally:
                    client.close()


class _ActionBatchBenchmark:
    def __init__(self, client: DaemonClient) -> None:
        self._client = client

    def run_batch(self) -> None:
        result = self._client.post_json(
            "/v1/actions/run",
            json={"actions": ACTION_BATCH_ACTIONS, "source": "benchmark"},
        )
        _ensure_ok_result(result)

    def run_separate(self) -> None:
        for action in ACTION_BATCH_ACTIONS:
            result = self._client.post_json(
                "/v1/actions/run",
                json={"actions": [action], "source": "benchmark"},
            )
            _ensure_ok_result(result)


class _ScreenshotBenchmark:
    def __init__(self, client: DaemonClient, request: dict[str, Any]) -> None:
        self._client = client
        self._request = request

    def run(self) -> dict[str, Any]:
        result = self._client.post_json("/v1/screenshots/full", json=self._request)
        return _safe_screenshot_result(result)


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


def _measure_observed_case(
    *,
    name: str,
    iterations: int,
    warmup_iterations: int,
    operation: Callable[[], Any],
    failures: list[dict[str, Any]],
) -> tuple[list[float], list[Any]]:
    samples: list[float] = []
    observations: list[Any] = []
    for warmup_index in range(warmup_iterations):
        try:
            operation()
        except Exception as exc:
            failures.append(_failure(name, phase="warmup", iteration=warmup_index, exc=exc))
            return samples, observations
    for iteration in range(iterations):
        start = time.perf_counter()
        try:
            observation = operation()
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            failures.append(
                _failure(name, phase="measure", iteration=iteration, exc=exc, elapsed_ms=elapsed_ms)
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


def _collect_metadata(client: DaemonClient, failures: list[dict[str, Any]]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    try:
        metadata["version"] = client.get_json("/v1/version")
    except Exception as exc:
        failures.append(_failure("metadata_version", phase="setup", iteration=0, exc=exc))
    try:
        capabilities = client.get_json("/v1/capabilities")
    except Exception as exc:
        failures.append(_failure("metadata_capabilities", phase="setup", iteration=0, exc=exc))
    else:
        metadata["capabilities"] = {
            "primitives": capabilities.get("primitives"),
            "screenshot_formats": capabilities.get("screenshot_formats"),
            "action_types": capabilities.get("action_types"),
            "image_profile": capabilities.get("image_profile"),
            "vnc_enabled": capabilities.get("vnc_enabled"),
        }
    return metadata


def _report_action_batch(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "failed" if result.get("failures") else "ok",
        "action_count": result.get("action_count"),
        "actions": result.get("actions"),
        "cases": result.get("cases"),
        "comparison": result.get("comparison"),
        "failures": result.get("failures", []),
    }


def _future_benchmark(status: FutureBenchmarkStatus, reason: str) -> dict[str, str]:
    return {"status": status, "reason": reason}


def _benchmark_failures(benchmark: str, failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(failure, benchmark=benchmark) for failure in failures]


def _safe_screenshot_request(request: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in request.items()
        if key not in {"bytes", "data_base64", "text", "clipboard", "token"}
    }


def _safe_screenshot_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError("daemon returned a non-object screenshot response")
    required = ("format", "width", "height", "size_bytes")
    missing = [key for key in required if key not in result]
    if missing:
        raise RuntimeError(f"daemon screenshot response missing fields: {', '.join(missing)}")
    return {
        "format": result["format"],
        "width": result["width"],
        "height": result["height"],
        "size_bytes": result["size_bytes"],
        "storage": "artifact" if result.get("artifact_uri") else "inline",
        "artifact_backed": result.get("artifact_uri") is not None,
        "cursor_visible": result.get("cursor_visible"),
    }


def _ensure_ok_result(result: Any) -> None:
    if not isinstance(result, dict):
        raise RuntimeError("daemon returned a non-object action response")
    if result.get("ok") is not True:
        raise RuntimeError("daemon action response was not ok")


def _failure(
    case: str,
    *,
    phase: str,
    iteration: int,
    exc: Exception,
    elapsed_ms: float | None = None,
) -> dict[str, Any]:
    failure: dict[str, Any] = {
        "case": case,
        "phase": phase,
        "iteration": iteration,
        "type": type(exc).__name__,
        "message": str(exc),
    }
    if elapsed_ms is not None:
        failure["elapsed_ms"] = elapsed_ms
    if isinstance(exc, DaemonHTTPError):
        failure["status_code"] = exc.status_code
        failure["code"] = exc.code
        failure["details"] = exc.details
    elif isinstance(exc, httpx.HTTPError):
        failure["code"] = "http_error"
    return failure

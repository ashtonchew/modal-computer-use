from __future__ import annotations

import math
import re
import statistics
import time
from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi.testclient import TestClient

from ._version import __version__
from .client import DaemonClient
from .daemon.app import create_app
from .daemon.settings import DaemonSettings
from .errors import DaemonHTTPError
from .transports.http import HTTPTransport

BenchmarkMode = Literal["mock-local", "http"]
ComparisonProvider = Literal[
    "modal-daemon",
    "modal-exec",
    "openai",
    "anthropic",
    "generic",
    "daytona",
    "e2b",
]
FutureBenchmarkStatus = Literal["not_measured", "unsupported"]
DEFAULT_COMPARE_PROVIDERS: tuple[ComparisonProvider, ...] = (
    "modal-daemon",
    "openai",
    "anthropic",
    "generic",
)
ACTION_BATCH_ACTIONS: list[dict[str, Any]] = [
    {"type": "move", "x": 10, "y": 10},
    {"type": "cursor_position"},
    {"type": "wait", "duration_ms": 0},
    {"type": "move", "x": 20, "y": 20},
    {"type": "cursor_position"},
]

MOVE_CLICK_ACTIONS: list[dict[str, Any]] = [
    {"type": "move", "x": 24, "y": 24},
    {"type": "click", "x": 24, "y": 24, "button": "left"},
]
TYPING_BENCHMARK_TEXT = "0123456789" * 10
TYPING_BENCHMARK_METHOD = "auto"
PROVIDER_BENCHMARK_TEXT = TYPING_BENCHMARK_TEXT
SANDBOX_EXEC_MOVE_CLICK_COMMAND: tuple[str, ...] = (
    "sh",
    "-lc",
    "command -v xdotool >/dev/null 2>&1 || exit 127; xdotool mousemove 24 24 click 1",
)


def run_benchmark_report(
    *,
    client: DaemonClient,
    mode: BenchmarkMode,
    iterations: int,
    base_url: str | None = None,
    warmup_iterations: int = 1,
    include_sandbox_exec: bool = False,
    sandbox_exec_runner: Callable[[tuple[str, ...], int], object] | None = None,
    sandbox_exec_setup_failure: dict[str, Any] | None = None,
    environment_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    failures: list[dict[str, Any]] = []
    metadata = _collect_metadata(client, failures)
    if environment_metadata:
        metadata["environment"] = {
            key: value for key, value in environment_metadata.items() if value is not None
        }
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
    move_click = run_move_click_benchmark(
        client=client,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    type_100_chars = run_type_100_chars_benchmark(
        client=client,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    recording_start_stop = run_recording_start_stop_benchmark(
        client=client,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    if include_sandbox_exec:
        sandbox_exec = run_sandbox_exec_benchmark(
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            runner=sandbox_exec_runner,
            setup_failure=sandbox_exec_setup_failure,
        )
    else:
        sandbox_exec = _future_benchmark(
            "not_measured",
            "Sandbox.exec comparison requires explicit --include-sandbox-exec live mode",
        )
    benchmarks = {
        "action_batch": _report_action_batch(action_batch),
        "screenshot_full": screenshot_full,
        "screenshot_compressed": screenshot_compressed,
        "move_click": move_click,
        "type_100_chars": type_100_chars,
        "recording_start_stop": recording_start_stop,
        "sandbox_exec": sandbox_exec,
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
    failures.extend(_benchmark_failures("move_click", move_click.get("failures", [])))
    failures.extend(_benchmark_failures("type_100_chars", type_100_chars.get("failures", [])))
    failures.extend(
        _benchmark_failures("recording_start_stop", recording_start_stop.get("failures", []))
    )
    if include_sandbox_exec:
        failures.extend(_benchmark_failures("sandbox_exec", sandbox_exec.get("failures", [])))
        if sandbox_exec.get("status") in {"ok", "failed"}:
            sandbox_exec["comparison"] = _named_case_comparison(
                "daemon_move_click",
                move_click,
                "sandbox_exec_move_click",
                sandbox_exec,
            )
    ok = not failures
    return {
        "ok": ok,
        "generated_at": datetime.now(UTC).isoformat(),
        "package_version": __version__,
        "mode": mode,
        "base_url": _safe_base_url(base_url),
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
    batch_samples, batch_observations = _measure_observed_case(
        name="batch_5_actions",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=benchmark.run_batch,
        failures=failures,
    )
    separate_samples, separate_observations = _measure_observed_case(
        name="separate_5_actions",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=benchmark.run_separate,
        failures=failures,
    )
    batch_case = _attributed_case_result(
        "batch_5_actions", iterations, batch_samples, batch_observations, failures
    )
    separate_case = _attributed_case_result(
        "separate_5_actions", iterations, separate_samples, separate_observations, failures
    )
    comparison = _comparison(batch_case, separate_case)
    ok = not failures
    return {
        "ok": ok,
        "benchmark": "action-batch",
        "timestamp": datetime.now(UTC).isoformat(),
        "package_version": __version__,
        "mode": mode,
        "base_url": _safe_base_url(base_url),
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


def run_provider_comparison(
    *,
    providers: list[ComparisonProvider] | None = None,
    iterations: int,
    client: DaemonClient | None = None,
    mode: BenchmarkMode | Literal["provider-live"] = "provider-live",
    base_url: str | None = None,
    warmup_iterations: int = 1,
    sandbox_exec_runner: Callable[[tuple[str, ...], int], object] | None = None,
    sandbox_exec_setup_failure: dict[str, Any] | None = None,
    environment_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .benchmark_comparison import run_provider_comparison as run_comparison

    return run_comparison(
        providers=providers,
        iterations=iterations,
        client=client,
        mode=mode,
        base_url=base_url,
        warmup_iterations=warmup_iterations,
        sandbox_exec_runner=sandbox_exec_runner,
        sandbox_exec_setup_failure=sandbox_exec_setup_failure,
        environment_metadata=environment_metadata,
    )


def run_provider_comparison_mock_local(
    *,
    providers: list[ComparisonProvider] | None = None,
    iterations: int,
    sandbox_exec_runner: Callable[[tuple[str, ...], int], object] | None = None,
    sandbox_exec_setup_failure: dict[str, Any] | None = None,
    environment_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _with_mock_local_client(
        lambda client: run_provider_comparison(
            providers=providers,
            client=client,
            mode="mock-local",
            iterations=iterations,
            base_url="http://testserver",
            sandbox_exec_runner=sandbox_exec_runner,
            sandbox_exec_setup_failure=sandbox_exec_setup_failure,
            environment_metadata=environment_metadata,
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


def run_type_100_chars_benchmark(
    *,
    client: DaemonClient,
    iterations: int,
    warmup_iterations: int = 1,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    failures: list[dict[str, Any]] = []
    benchmark = _Type100CharsBenchmark(client, TYPING_BENCHMARK_TEXT)
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


def run_sandbox_exec_benchmark(
    *,
    iterations: int,
    warmup_iterations: int = 1,
    runner: Callable[[tuple[str, ...], int], object] | None,
    setup_failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    failures: list[dict[str, Any]] = []
    if setup_failure is not None:
        failures.append(setup_failure)
    elif runner is None:
        failures.append(
            {
                "case": "sandbox_exec_move_click",
                "phase": "setup",
                "iteration": 0,
                "type": "RuntimeError",
                "message": "Sandbox.exec runner was not configured",
                "code": "sandbox_exec_not_configured",
            }
        )
    if failures:
        result = _case_result("sandbox_exec_move_click", iterations, [], failures)
    else:
        benchmark = _SandboxExecBenchmark(runner)
        samples = _measure_case(
            name="sandbox_exec_move_click",
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            operation=benchmark.run,
            failures=failures,
        )
        result = _case_result("sandbox_exec_move_click", iterations, samples, failures)
    result.update(
        {
            "command": {
                "tool": "xdotool",
                "action_count": len(MOVE_CLICK_ACTIONS),
                "actions": [_safe_action_metadata(action) for action in MOVE_CLICK_ACTIONS],
                "timeout_seconds": 10,
            }
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
                    input_rate_limit_per_sec=0,
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

    def run_batch(self) -> dict[str, float | None]:
        result = self._client.post_json(
            "/v1/actions/run",
            json={"actions": ACTION_BATCH_ACTIONS, "source": "benchmark"},
        )
        _ensure_ok_result(result)
        return {"daemon_ms": _extract_daemon_ms(result)}

    def run_separate(self) -> dict[str, float | None]:
        daemon_samples: list[float | None] = []
        for action in ACTION_BATCH_ACTIONS:
            result = self._client.post_json(
                "/v1/actions/run",
                json={"actions": [action], "source": "benchmark"},
            )
            _ensure_ok_result(result)
            daemon_samples.append(_extract_daemon_ms(result))
        if any(sample is None for sample in daemon_samples):
            return {"daemon_ms": None}
        return {"daemon_ms": sum(sample for sample in daemon_samples if sample is not None)}


class _MoveClickBenchmark:
    def __init__(self, client: DaemonClient) -> None:
        self._client = client

    def run(self) -> dict[str, float | None]:
        result = self._client.post_json(
            "/v1/actions/run",
            json={"actions": MOVE_CLICK_ACTIONS, "source": "benchmark"},
        )
        _ensure_ok_result(result)
        return {"daemon_ms": _extract_daemon_ms(result)}


class _Type100CharsBenchmark:
    def __init__(self, client: DaemonClient, text: str) -> None:
        self._client = client
        self._text = text

    def run(self) -> dict[str, float | None]:
        result = self._client.post_json(
            "/v1/actions/run",
            json={
                "actions": [
                    {
                        "type": "type",
                        "text": self._text,
                        "method": TYPING_BENCHMARK_METHOD,
                    }
                ],
                "source": "benchmark",
            },
        )
        _ensure_ok_result(result)
        return {"daemon_ms": _extract_daemon_ms(result)}


class _ScreenshotBenchmark:
    def __init__(self, client: DaemonClient, request: dict[str, Any]) -> None:
        self._client = client
        self._request = request

    def run(self) -> dict[str, Any]:
        result = self._client.post_json("/v1/screenshots/full", json=self._request)
        return _safe_screenshot_result(result)


class _RecordingStartStopBenchmark:
    def __init__(self, client: DaemonClient) -> None:
        self._client = client

    def run(self) -> dict[str, Any]:
        started = self.start()
        return self.stop(started)

    def start(self) -> Any:
        return self._client.post_json(
            "/v1/recordings",
            json={"name": "benchmark", "fps": 5, "format": "mp4"},
        )

    def stop(self, started: Any) -> dict[str, Any]:
        recording_id = _recording_id(started)
        stopped = self._client.post_json(f"/v1/recordings/{recording_id}/stop")
        return _safe_recording_result(stopped)


class _SandboxExecBenchmark:
    def __init__(self, runner: Callable[[tuple[str, ...], int], object]) -> None:
        self._runner = runner

    def run(self) -> None:
        try:
            process = self._runner(SANDBOX_EXEC_MOVE_CLICK_COMMAND, 10)
        except Exception as exc:
            if _is_timeout_exception(exc):
                raise _SandboxExecBenchmarkError(
                    "sandbox_exec_timeout",
                    "Sandbox.exec command timed out",
                ) from exc
            raise _SandboxExecBenchmarkError(
                "sandbox_exec_start_failed",
                "Sandbox.exec failed before returning a process handle",
            ) from exc
        try:
            wait = getattr(process, "wait", None)
            wait_result = wait() if callable(wait) else None
        except Exception as exc:
            if not _is_timeout_exception(exc):
                raise _SandboxExecBenchmarkError(
                    "sandbox_exec_wait_failed",
                    "Sandbox.exec process wait failed",
                ) from exc
            raise _SandboxExecBenchmarkError(
                "sandbox_exec_timeout",
                "Sandbox.exec command timed out",
            ) from exc
        return_code = getattr(process, "returncode", None)
        if return_code is None and isinstance(wait_result, int):
            return_code = wait_result
        if return_code == 127:
            raise _SandboxExecBenchmarkError(
                "sandbox_exec_missing_tool",
                "Sandbox.exec command could not find xdotool in the sandbox",
            )
        if return_code not in (None, 0):
            raise _SandboxExecBenchmarkError(
                "sandbox_exec_nonzero_exit",
                "Sandbox.exec command exited nonzero",
            )


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
    benchmark: _RecordingStartStopBenchmark,
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


def _safe_base_url(base_url: str | None) -> str | None:
    if base_url is None:
        return None
    parsed = urlsplit(base_url)
    netloc = parsed.netloc
    if parsed.username or parsed.password:
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        netloc = f"{hostname}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _benchmark_failures(benchmark: str, failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(failure, benchmark=benchmark) for failure in failures]


def _safe_screenshot_request(request: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in request.items()
        if key not in {"bytes", "data_base64", "text", "clipboard", "token"}
    }


def _safe_action_metadata(action: dict[str, Any]) -> dict[str, Any]:
    metadata = {"type": action.get("type") or action.get("action") or "unknown"}
    if "button" in action:
        metadata["button"] = action["button"]
    return metadata


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


def _recording_id(result: Any) -> str:
    if not isinstance(result, dict):
        raise RuntimeError("daemon returned a non-object recording start response")
    recording_id = result.get("id")
    if not isinstance(recording_id, str) or not recording_id:
        raise RuntimeError("daemon recording start response missing id")
    return recording_id


def _safe_recording_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError("daemon returned a non-object recording stop response")
    required = ("status", "format", "size_bytes")
    missing = [key for key in required if key not in result]
    if missing:
        raise RuntimeError(f"daemon recording stop response missing fields: {', '.join(missing)}")
    if result["status"] != "stopped":
        raise RuntimeError(f"daemon recording status was {result['status']}")
    return {
        "status": result["status"],
        "format": result["format"],
        "fps": result.get("fps"),
        "size_bytes": result["size_bytes"],
        "artifact_backed": result.get("artifact_uri") is not None,
        "duration_seconds": result.get("duration_seconds"),
        "stop_method": result.get("stop_method"),
        "return_code": result.get("return_code"),
    }


def _ensure_ok_result(result: Any) -> None:
    if not isinstance(result, dict):
        raise RuntimeError("daemon returned a non-object action response")
    if result.get("ok") is not True:
        raise RuntimeError("daemon action response was not ok")


def _extract_daemon_ms(result: dict[str, Any]) -> float | None:
    timing = result.get("timing")
    if timing is None:
        return None
    if not isinstance(timing, dict):
        raise RuntimeError("daemon action timing was malformed")
    daemon_ms = timing.get("daemon_ms")
    if isinstance(daemon_ms, bool) or not isinstance(daemon_ms, int | float):
        raise RuntimeError("daemon action timing.daemon_ms was malformed")
    if daemon_ms < 0:
        raise RuntimeError("daemon action timing.daemon_ms was negative")
    return float(daemon_ms)


def _failure(
    case: str,
    *,
    phase: str,
    iteration: int,
    exc: Exception,
    elapsed_ms: float | None = None,
    redacted_text: str | None = None,
) -> dict[str, Any]:
    failure: dict[str, Any] = {
        "case": case,
        "phase": phase,
        "iteration": iteration,
        "type": type(exc).__name__,
        "message": _redact_text(str(exc), redacted_text),
    }
    if elapsed_ms is not None:
        failure["elapsed_ms"] = elapsed_ms
    if isinstance(exc, DaemonHTTPError):
        failure["status_code"] = exc.status_code
        failure["code"] = exc.code
        failure["details"] = _redact_text(exc.details, redacted_text)
    elif isinstance(exc, _SandboxExecBenchmarkError):
        failure["code"] = exc.code
    elif isinstance(exc, httpx.HTTPError):
        failure["code"] = "http_error"
    return failure


def _redact_text(value: Any, redacted_text: str | None) -> Any:
    if isinstance(value, str):
        output = value
        if redacted_text:
            output = output.replace(redacted_text, "[redacted typed text]")
        replacements = [
            (r"(?i)(authorization:\s*bearer\s+)[^\s,;]+", r"\1[redacted]"),
            (r"(?i)(bearer\s+)[a-z0-9._~+/=-]+", r"\1[redacted]"),
            (r"(?i)(api[_-]?key['\"]?\s*[:=]\s*['\"]?)[^'\"\s,;]+", r"\1[redacted]"),
            (r"(?i)(token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,;]+", r"\1[redacted]"),
            (r"(https?://[^?\s]+)\?[^ \n\t]+", r"\1?[redacted-query]"),
        ]
        for pattern, replacement in replacements:
            output = re.sub(pattern, replacement, output)
        return output
    if isinstance(value, list):
        return [_redact_text(item, redacted_text) for item in value]
    if isinstance(value, dict):
        return {
            ("redacted_text" if key == "text" else key): _redact_text(item, redacted_text)
            for key, item in value.items()
        }
    return value


def _is_timeout_exception(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__


class _SandboxExecBenchmarkError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .._version import __version__
from ..client import DaemonClient
from .constants import (
    ACTION_BATCH_ACTIONS,
    COORDINATE_CLICK_SEQUENCE_ACTIONS,
    BenchmarkMode,
)
from .measurement import _attributed_case_result, _comparison, _measure_observed_case
from .mock_local import _with_mock_local_client
from .operations import _ActionBatchBenchmark, _FourClickBatchBenchmark
from .safety import _safe_base_url


def run_action_batch_benchmark(
    *,
    client: DaemonClient,
    mode: BenchmarkMode,
    iterations: int,
    base_url: str | None = None,
    warmup_iterations: int = 1,
    before_iteration: Callable[[], None] | None = None,
    include_legacy_cases: bool = True,
    include_four_click_cases: bool = False,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    failures: list[dict[str, Any]] = []
    cases: dict[str, Any] = {}
    comparison: dict[str, Any] = {"status": "not_measured"}
    if include_legacy_cases:
        benchmark = _ActionBatchBenchmark(client)
        batch_case = _measure_action_case(
            name="batch_5_actions",
            operation=benchmark.run_batch,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            failures=failures,
            before_iteration=before_iteration,
        )
        separate_case = _measure_action_case(
            name="separate_5_actions",
            operation=benchmark.run_separate,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            failures=failures,
            before_iteration=before_iteration,
        )
        cases.update(
            {
                "batch_5_actions": batch_case,
                "separate_5_actions": separate_case,
                "sandbox_exec": {
                    "status": "not_measured",
                    "reason": "Sandbox.exec comparison is unsupported in this benchmark pass",
                },
            }
        )
        comparison = _comparison(batch_case, separate_case)

    four_click_comparison: dict[str, Any] = {"status": "not_measured"}
    if include_four_click_cases:
        four_click = _FourClickBatchBenchmark(client)
        batch_4 = _measure_action_case(
            name="batch_4_clicks",
            operation=four_click.run_batch,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            failures=failures,
            before_iteration=before_iteration,
        )
        separate_4 = _measure_action_case(
            name="separate_4_clicks",
            operation=four_click.run_separate,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            failures=failures,
            before_iteration=before_iteration,
        )
        batch_4.update(_four_click_case_metadata(sdk_calls=1))
        separate_4.update(_four_click_case_metadata(sdk_calls=4))
        cases.update({"batch_4_clicks": batch_4, "separate_4_clicks": separate_4})
        four_click_comparison = _p50_comparison(batch_4, separate_4)
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
        "action_count": (
            len(ACTION_BATCH_ACTIONS)
            if include_legacy_cases
            else len(COORDINATE_CLICK_SEQUENCE_ACTIONS)
        ),
        "actions": [
            {"type": action["type"]}
            for action in (
                ACTION_BATCH_ACTIONS
                if include_legacy_cases
                else COORDINATE_CLICK_SEQUENCE_ACTIONS
            )
        ],
        "measurement_policy": {
            "timer_boundary": "complete arm at caller",
            "retries": 0,
            "replacement_samples": 0,
            "fixed_action_order": True,
        },
        "cases": cases,
        "comparison": comparison,
        "four_click_comparison": four_click_comparison,
        "failures": failures,
    }


def _measure_action_case(
    *,
    name: str,
    operation: Callable[[], dict[str, Any]],
    iterations: int,
    warmup_iterations: int,
    failures: list[dict[str, Any]],
    before_iteration: Callable[[], None] | None,
) -> dict[str, Any]:
    samples, observations = _measure_observed_case(
        name=name,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=operation,
        failures=failures,
        before_iteration=before_iteration,
    )
    return _attributed_case_result(name, iterations, samples, observations, failures)


def _four_click_case_metadata(*, sdk_calls: int) -> dict[str, Any]:
    return {
        "logical_action_count": 4,
        "sdk_call_count": sdk_calls,
        "transport_request_count": sdk_calls,
        "batching_semantics": (
            "one ordered action batch, validated before execution, stop on first error"
            if sdk_calls == 1
            else "four sequential requests in fixed order, stop on first error"
        ),
        "timer_boundary": "before first SDK call through validation of final response",
        "actions": [
            {
                "type": action["type"],
                "x": action["x"],
                "y": action["y"],
                "button": action["button"],
            }
            for action in COORDINATE_CLICK_SEQUENCE_ACTIONS
        ],
    }


def _p50_comparison(batch_case: dict[str, Any], separate_case: dict[str, Any]) -> dict[str, Any]:
    batch_p50 = batch_case["summary_ms"]["p50"]
    separate_p50 = separate_case["summary_ms"]["p50"]
    if batch_p50 in (None, 0) or separate_p50 is None:
        return {"status": "not_available", "speedup": None, "delta_ms": None}
    return {
        "status": "measured",
        "metric": "p50",
        "batch_p50_ms": batch_p50,
        "separate_p50_ms": separate_p50,
        "speedup": separate_p50 / batch_p50,
        "delta_ms": separate_p50 - batch_p50,
        "batch_faster": batch_p50 < separate_p50,
    }


def run_action_batch_benchmark_mock_local(
    *,
    iterations: int,
    warmup_iterations: int = 1,
    include_legacy_cases: bool = True,
    include_four_click_cases: bool = False,
) -> dict[str, Any]:
    return _with_mock_local_client(
        lambda client: run_action_batch_benchmark(
            client=client,
            mode="mock-local",
            iterations=iterations,
            base_url="http://testserver",
            warmup_iterations=warmup_iterations,
            include_legacy_cases=include_legacy_cases,
            include_four_click_cases=include_four_click_cases,
        )
    )

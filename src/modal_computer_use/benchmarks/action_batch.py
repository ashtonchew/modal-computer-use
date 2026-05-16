from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .._version import __version__
from ..client import DaemonClient
from .constants import ACTION_BATCH_ACTIONS, BenchmarkMode
from .measurement import _attributed_case_result, _comparison, _measure_observed_case
from .mock_local import _with_mock_local_client
from .operations import _ActionBatchBenchmark
from .safety import _safe_base_url


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

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .constants import MOVE_CLICK_ACTIONS
from .measurement import _case_result, _measure_case
from .operations import _SandboxExecBenchmark
from .safety import _safe_action_metadata


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

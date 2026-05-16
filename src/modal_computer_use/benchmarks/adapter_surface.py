from __future__ import annotations

import time
from typing import Any

from ..adapters.anthropic import AnthropicAdapter
from ..adapters.generic import ActionExecutor
from ..adapters.openai import OpenAIAdapter
from ..models import ActionBatchResult, ActionItemResult
from . import core
from .constants import BenchmarkSurface
from .surface_result import _surface_result


def _run_adapter_surface(
    *,
    surface: BenchmarkSurface,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    benchmark = _AdapterSurfaceBenchmark(surface)
    samples, observations = core._measure_observed_case(
        name=f"{surface}_matrix",
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=benchmark.run,
        failures=failures,
        redacted_text=core.ADAPTER_BENCHMARK_TEXT,
    )
    case = core._case_result(f"{surface}_matrix", iterations, samples, failures)
    case.update(
        {
            "actions": observations[-1]["actions"] if observations else benchmark.safe_actions,
            "action_count": len(benchmark.safe_actions),
            "last_result": observations[-1] if observations else None,
        }
    )
    return _surface_result(
        surface,
        cases={"adapter_matrix": case},
        metadata=benchmark.metadata,
        runtime_seconds=None,
    )

class _AdapterSurfaceBenchmark:
    def __init__(self, surface: BenchmarkSurface) -> None:
        self.surface = surface
        self.metadata = self._metadata()
        self.safe_actions = [core._safe_action_metadata(action) for action in self._actions()]

    def run(self) -> dict[str, Any]:
        computer = _BenchmarkRecordingComputer()
        actions = self._actions()
        start = time.perf_counter()
        if self.surface == "openai-adapter":
            OpenAIAdapter(computer).apply_many(actions)
        elif self.surface == "anthropic-adapter":
            AnthropicAdapter(computer, tool_version="computer_20250124").apply_many(actions)
        elif self.surface == "action-executor":
            ActionExecutor(computer).apply_many(actions)
        else:
            raise RuntimeError(f"unsupported adapter surface: {self.surface}")
        elapsed_ms = (time.perf_counter() - start) * 1000
        run = computer.actions.runs[-1]
        return {
            "elapsed_ms": elapsed_ms,
            "source": run["source"],
            "actions": [core._safe_action_metadata(action) for action in run["actions"]],
        }

    def _actions(self) -> list[dict[str, Any]]:
        if self.surface == "openai-adapter":
            return [
                {"type": "move", "x": 10, "y": 20},
                {"type": "click", "x": 10, "y": 20, "button": "left"},
                {"type": "type", "text": core.ADAPTER_BENCHMARK_TEXT},
                {"type": "wait", "duration_ms": 0},
            ]
        if self.surface == "anthropic-adapter":
            return [
                {"action": "mouse_move", "coordinate": [10, 20]},
                {"action": "left_click", "coordinate": [10, 20]},
                {"action": "type", "text": core.ADAPTER_BENCHMARK_TEXT},
                {"action": "wait", "duration_ms": 0},
            ]
        return [
            {"type": "move", "x": 10, "y": 20},
            {"type": "click", "x": 10, "y": 20, "button": "left"},
            {"type": "type", "text": core.ADAPTER_BENCHMARK_TEXT},
            {"type": "wait", "duration_ms": 0},
        ]

    def _metadata(self) -> dict[str, Any]:
        if self.surface == "anthropic-adapter":
            return {
                "adapter": "AnthropicAdapter",
                "tool_version": "computer_20250124",
                "provider_api_calls": False,
            }
        if self.surface == "openai-adapter":
            return {"adapter": "OpenAIAdapter", "provider_api_calls": False}
        return {"executor": "ActionExecutor", "provider_api_calls": False}

class _BenchmarkRecordingActions:
    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []

    def run(
        self,
        actions: list[Any],
        *,
        continue_on_error: bool = False,
        screenshot_after: bool = False,
        source: str = "sdk",
    ) -> ActionBatchResult:
        dumped = [action.model_dump(mode="json") for action in actions]
        self.runs.append(
            {
                "actions": dumped,
                "continue_on_error": continue_on_error,
                "screenshot_after": screenshot_after,
                "source": source,
            }
        )
        return ActionBatchResult(
            ok=True,
            results=[
                ActionItemResult(index=index, type=action["type"], ok=True)
                for index, action in enumerate(dumped)
            ],
        )

    def apply(self, action: Any) -> Any:
        return self.run([action]).results[0]

class _BenchmarkRecordingComputer:
    def __init__(self) -> None:
        self.actions = _BenchmarkRecordingActions()

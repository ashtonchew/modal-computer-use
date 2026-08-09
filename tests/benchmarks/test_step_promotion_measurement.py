from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from modal_computer_use.benchmarks.step_promotion_gate import (
    CANDIDATE_ARM,
    PRIOR_PUBLIC_ARM,
)
from modal_computer_use.benchmarks.step_promotion_measurement import (
    measure_interleaved_step_promotion,
)
from modal_computer_use.models import (
    ActionBatchResult,
    ActionBatchTiming,
    ActionItemResult,
    CoordinateSpace,
    Screenshot,
)
from modal_computer_use.steps import ComputerStepTiming


def _configuration() -> dict[str, object]:
    return {
        "caller_topology": "one-application-owned-modal-function",
        "target_identity": "deterministic-color-target-v1",
        "requested_placement": {"cloud": "aws", "region": "us-west-2"},
        "observed_placement": {
            "function": {"cloud": "aws", "region": "us-west-2"},
            "target": {"cloud": "aws", "region": "us-west-2"},
        },
        "resources": {
            "function": {"cpu": 1, "memory_mib": 2048},
            "sandbox": {"cpu": 1, "memory_mib": 2048},
        },
        "image_identity": "image-sha-abc123",
        "ingress": "attested-tunnel",
        "http_version": "1.1",
        "input_backend": "xtest",
        "input_rate_limit_per_sec": 20,
        "operation_pacing_ms": 125,
        "screenshot": {
            "format": "png",
            "quality": 90,
            "scale": 1.0,
            "show_cursor": False,
            "processing": "daemon",
            "storage": "inline",
            "transport": "raw-binary",
        },
        "action_scenario": "reset-then-click-color-target-v1",
        "connection_reuse": "one-pooled-async-client",
        "warm_capacity": {"function_min_containers": 0, "sandbox_pool_capacity": 0},
    }


def _screenshot(token: str) -> Screenshot:
    payload = f"frame:{token}".encode()
    return Screenshot(
        format="png",
        width=1,
        height=1,
        size_bytes=len(payload),
        bytes=payload,
        coordinate_space=CoordinateSpace.from_dimensions(desktop_width=1, desktop_height=1),
    )


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.001
        return self.value


async def _no_sleep(seconds: float) -> None:
    assert seconds == 0.125


class _Computer:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.expected = "initial"
        self.actions = SimpleNamespace(run=self.run)
        self.screenshots = SimpleNamespace(full=self.full)

    async def run(self, actions: list[dict[str, Any]], **kwargs: object) -> ActionBatchResult:
        self.events.append("prior-action")
        assert kwargs["continue_on_error"] is False
        assert kwargs["screenshot_after"] is False
        return ActionBatchResult(
            ok=True,
            results=[
                ActionItemResult(
                    index=index,
                    type=str(action["type"]),
                    ok=True,
                    output={"input_backend": "xtest"},
                )
                for index, action in enumerate(actions)
            ],
            timing=ActionBatchTiming(daemon_ms=0.5),
        )

    async def full(self, **_kwargs: object) -> Screenshot:
        self.events.append("prior-screenshot")
        return _screenshot(self.expected)

    async def step(self, actions: list[dict[str, Any]], **kwargs: object) -> Any:
        self.events.append("candidate-step")
        assert kwargs["continue_on_error"] is False
        options = kwargs["screenshot_options"]
        assert options.format == "png"
        assert options.quality == 90
        assert options.scale == 1.0
        assert options.show_cursor is False
        assert options.processing == "daemon"
        assert options.storage == "inline"
        return SimpleNamespace(
            actions=ActionBatchResult(
                ok=True,
                results=[
                    ActionItemResult(
                        index=index,
                        type=str(action["type"]),
                        ok=True,
                        output={"input_backend": "xtest"},
                    )
                    for index, action in enumerate(actions)
                ],
                timing=ActionBatchTiming(daemon_ms=0.5),
            ),
            screenshot=_screenshot(self.expected),
            timing=ComputerStepTiming(
                daemon_ms=0.75,
                action_ms=0.25,
                screenshot_ms=0.5,
                total_ms=0.75,
            ),
        )


@pytest.mark.asyncio
async def test_step_measurement_uses_one_borrow_and_exact_arm_operation_order() -> None:
    events: list[str] = []
    computer = _Computer(events)
    borrow_count = 0

    @asynccontextmanager
    async def borrow() -> Any:
        nonlocal borrow_count
        borrow_count += 1
        events.append("borrow-enter")
        try:
            yield computer
        finally:
            events.append("borrow-exit")

    async def prepare(target: _Computer, pair_index: int, arm: str) -> str:
        token = f"{pair_index}:{arm}"
        target.expected = token
        events.append(f"prepare:{arm}")
        return token

    def verify(frame: Screenshot, token: object) -> bool:
        return frame.as_bytes() == f"frame:{token}".encode()

    artifacts = await measure_interleaved_step_promotion(
        borrow,
        actions=[{"type": "click", "x": 32, "y": 32}],
        prepare=prepare,
        verify_frame=verify,
        configuration=_configuration(),
        lifecycle_timings={"cold_start_ms": 1.0, "startup_ms": 2.0, "dispatch_ms": 3.0},
        sample_count=100,
        warmup_iterations=2,
        schedule_seed=42,
        bootstrap_seed=7,
        bootstrap_resamples=500,
        operation_pacing_seconds=0.125,
        sleeper=_no_sleep,
        clock=_Clock(),
    )

    assert borrow_count == 1
    assert events[0] == "borrow-enter"
    assert events[-1] == "borrow-exit"
    for index, event in enumerate(events):
        if event == "prior-action":
            assert events[index + 1] == "prior-screenshot"
        if event == "candidate-step":
            assert events[index - 1] == f"prepare:{CANDIDATE_ARM}"
    assert len(artifacts[PRIOR_PUBLIC_ARM]["observations"]) == 100
    assert len(artifacts[CANDIDATE_ARM]["observations"]) == 100
    assert all(
        observation["freshness_verified"] is True
        for artifact in artifacts.values()
        for observation in artifact["observations"]
    )
    assert all(artifact["retries"] == 0 for artifact in artifacts.values())
    assert all(artifact["replacement_samples"] == 0 for artifact in artifacts.values())


@pytest.mark.asyncio
async def test_step_measurement_stops_after_first_freshness_failure() -> None:
    computer = _Computer([])

    @asynccontextmanager
    async def borrow() -> Any:
        yield computer

    async def prepare(target: _Computer, pair_index: int, arm: str) -> str:
        target.expected = f"{pair_index}:{arm}"
        return target.expected

    artifacts = await measure_interleaved_step_promotion(
        borrow,
        actions=[{"type": "click", "x": 32, "y": 32}],
        prepare=prepare,
        verify_frame=lambda _frame, _token: False,
        configuration=_configuration(),
        lifecycle_timings={"cold_start_ms": 1.0, "startup_ms": 2.0, "dispatch_ms": 3.0},
        sample_count=100,
        warmup_iterations=0,
        schedule_seed=42,
        bootstrap_seed=7,
        bootstrap_resamples=500,
        operation_pacing_seconds=0.125,
        sleeper=_no_sleep,
    )

    assert sum(len(artifact["observations"]) for artifact in artifacts.values()) == 1
    assert sum(len(artifact["failures"]) for artifact in artifacts.values()) == 1
    assert all(artifact["status"] == "failed" for artifact in artifacts.values())


@pytest.mark.asyncio
async def test_step_measurement_rejects_missing_candidate_phase_timings() -> None:
    computer = _Computer([])
    original_step = computer.step

    async def step_without_phases(actions: list[dict[str, Any]], **kwargs: object) -> Any:
        result = await original_step(actions, **kwargs)
        result.timing = ComputerStepTiming(daemon_ms=0.75)
        return result

    computer.step = step_without_phases  # type: ignore[method-assign]

    @asynccontextmanager
    async def borrow() -> Any:
        yield computer

    async def prepare(target: _Computer, pair_index: int, arm: str) -> str:
        target.expected = f"{pair_index}:{arm}"
        return target.expected

    artifacts = await measure_interleaved_step_promotion(
        borrow,
        actions=[{"type": "click", "x": 32, "y": 32}],
        prepare=prepare,
        verify_frame=lambda frame, token: (
            frame.as_bytes() == f"frame:{token}".encode()
        ),
        configuration=_configuration(),
        lifecycle_timings={"cold_start_ms": 1.0, "startup_ms": 2.0, "dispatch_ms": 3.0},
        sample_count=100,
        warmup_iterations=0,
        schedule_seed=42,
        bootstrap_seed=7,
        bootstrap_resamples=500,
        operation_pacing_seconds=0.125,
        sleeper=_no_sleep,
    )

    candidate = artifacts[CANDIDATE_ARM]
    assert candidate["status"] == "failed"
    assert candidate["failures"][0]["error_category"] == "attribution"


@pytest.mark.asyncio
async def test_step_measurement_records_cleanup_failure_for_both_arms() -> None:
    computer = _Computer([])

    @asynccontextmanager
    async def borrow() -> Any:
        yield computer
        raise RuntimeError("private cleanup detail")

    async def prepare(target: _Computer, pair_index: int, arm: str) -> str:
        target.expected = f"{pair_index}:{arm}"
        return target.expected

    artifacts = await measure_interleaved_step_promotion(
        borrow,
        actions=[{"type": "click", "x": 32, "y": 32}],
        prepare=prepare,
        verify_frame=lambda frame, token: frame.as_bytes() == f"frame:{token}".encode(),
        configuration=_configuration(),
        lifecycle_timings={"cold_start_ms": 1.0, "startup_ms": 2.0, "dispatch_ms": 3.0},
        sample_count=100,
        warmup_iterations=0,
        schedule_seed=42,
        bootstrap_seed=7,
        bootstrap_resamples=500,
        operation_pacing_seconds=0.125,
        sleeper=_no_sleep,
    )

    assert all(artifact["status"] == "failed" for artifact in artifacts.values())
    assert all(
        artifact["failures"][-1] == {
            "phase": "cleanup",
            "sample_index": None,
            "status": "failed",
            "error_category": "cleanup",
        }
        for artifact in artifacts.values()
    )

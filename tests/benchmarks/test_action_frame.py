from __future__ import annotations

from typing import Any

import pytest

from modal_computer_use.benchmark_comparison import run_provider_comparison
from modal_computer_use.benchmarks.provider_comparison.action_frame import (
    ACTION_FRAME_CASE_ID,
    ACTION_FRAME_TIMER_BOUNDARY,
    run_action_to_immediate_frame_case,
)
from modal_computer_use.benchmarks.provider_comparison.live import run_product_provider_cases


class _Driver:
    def __init__(self, observations: list[dict[str, Any]]) -> None:
        self._observations = iter(observations)
        self.calls = 0

    def action_to_immediate_frame(self, _resource: object) -> dict[str, Any]:
        self.calls += 1
        return next(self._observations)


def _observation(*, value: int = 1) -> dict[str, Any]:
    return {
        "path": "provider-sdk-action-then-screenshot",
        "actions": {
            "case_id": ACTION_FRAME_CASE_ID,
            "logical_action_count": 1,
            "provider_action_count": 1,
            "provider_sdk_call_count": 1,
            "transport_request_count": 1,
            "batching": "single_request",
        },
        "screenshot": {
            "format": "png",
            "width": 1024,
            "height": 768,
            "show_cursor": False,
            "decoded_size_bytes": 100 + value,
            "transport_encoding": "raw_bytes",
        },
    }


def test_action_frame_measurement_uses_one_timer_for_actions_and_screenshot() -> None:
    driver = _Driver([_observation(value=1), _observation(value=2)])

    result = run_action_to_immediate_frame_case(
        provider="daytona",
        driver=driver,
        resource=object(),
        iterations=2,
        warmup_iterations=0,
    )

    assert result["status"] == "ok"
    assert result["case_id"] == ACTION_FRAME_CASE_ID
    assert result["timer_boundary"] == ACTION_FRAME_TIMER_BOUNDARY
    assert result["screenshot"] == {
        "format": "png",
        "width": 1024,
        "height": 768,
        "show_cursor": False,
    }
    assert result["request_shape"] == {
        "sdk_calls": 2,
        "transport_requests": 2,
        "batching": "single-request",
        "action_sdk_calls": 1,
        "action_transport_requests": 1,
        "screenshot_sdk_calls": 1,
        "screenshot_transport_requests": 1,
    }
    assert result["harness_retries"] == 0
    assert result["replacement_samples"] == 0
    assert driver.calls == 2


def test_action_frame_measurement_keeps_failures_without_replaying_mutation() -> None:
    class FailingDriver:
        calls = 0

        def action_to_immediate_frame(self, _resource: object) -> dict[str, Any]:
            self.calls += 1
            raise RuntimeError("screenshot failed after action dispatch")

    driver = FailingDriver()

    result = run_action_to_immediate_frame_case(
        provider="e2b",
        driver=driver,
        resource=object(),
        iterations=1,
        warmup_iterations=0,
    )

    assert result["status"] == "failed"
    assert result["successful_iterations"] == 0
    assert result["harness_retries"] == 0
    assert result["replacement_samples"] == 0
    assert driver.calls == 1
    assert len(result["failures"]) == 1
    assert "screenshot failed" not in str(result["failures"])


def test_action_frame_requires_a_validated_screenshot() -> None:
    driver = _Driver(
        [
            {
                "actions": {},
                "screenshot": {"format": "png", "width": 0, "height": 768},
            }
        ]
    )

    result = run_action_to_immediate_frame_case(
        provider="tzafon",
        driver=driver,
        resource=object(),
        iterations=1,
        warmup_iterations=0,
    )

    assert result["status"] == "failed"
    assert result["successful_iterations"] == 0
    assert result["failures"]


def test_action_frame_rejects_invalid_iteration_count() -> None:
    with pytest.raises(ValueError, match="iterations must be >= 1"):
        run_action_to_immediate_frame_case(
            provider="daytona",
            driver=_Driver([]),
            resource=object(),
            iterations=0,
            warmup_iterations=0,
        )


def test_action_frame_selector_runs_only_the_new_case() -> None:
    class Driver(_Driver):
        action_verification_calls = 0

        def create_lifecycle_session(self) -> object:
            return object()

        def observe_first_screenshot(self, _resource: object) -> dict[str, Any]:
            return {"status": "ready"}

        def cleanup_session(self, _resource: object) -> list[tuple[str, Exception]]:
            return []

        def screenshot_full(self, _resource: object) -> dict[str, Any]:
            raise AssertionError("legacy warm cases must not run")

        def verify_readbacks(self, _resource: object) -> dict[str, Any]:
            raise AssertionError("legacy cursor and type verification must not run")

        def verify_action_frame_readback(self, _resource: object) -> dict[str, Any]:
            self.action_verification_calls += 1
            return {
                "cursor_position": {
                    "status": "ok",
                    "expected": {"x": 512, "y": 384},
                    "observed": {"x": 512, "y": 384},
                }
            }

    driver = Driver([_observation()])
    result = run_product_provider_cases(
        provider="daytona",
        driver=driver,
        cold_cases=("cold_create_to_ready",),
        warm_cases=("screenshot_full", "command_echo"),
        iterations=1,
        warmup_iterations=0,
        metadata={},
        benchmark_case="action-to-immediate-frame",
    )

    assert set(result["cases"]) == {"action_to_immediate_frame"}
    assert driver.calls == 1
    assert driver.action_verification_calls == 1
    assert result["verification"]["cursor_position"]["status"] == "ok"


def test_default_provider_comparison_does_not_add_action_frame_case() -> None:
    payload = run_provider_comparison(
        providers=["daytona"],
        mode="mock-local",
        iterations=1,
        warmup_iterations=0,
    )

    assert "action_to_immediate_frame" not in payload["providers"]["daytona"]["cases"]

"""Action-to-immediate-frame measurements for provider comparisons.

The operation measured here is one complete provider path.  The timer starts
before the ordered action dispatch and ends after the caller has received and
validated screenshot bytes.  The helper does not retry an operation because a
provider may have dispatched the mutation before observation failed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from ..measurement import _case_result, _measure_observed_case
from .provider_sdk import sanitize_provider_observation

ACTION_FRAME_CASE = "action_to_immediate_frame"
ACTION_FRAME_CASE_ID = "ordered-actions-to-immediate-frame-v1"
ACTION_FRAME_SEMANTICS = "one-left-click-at-512-384-then-immediate-full-frame"
ACTION_FRAME_TIMER_BOUNDARY = (
    "caller_before_ordered_action_dispatch_to_validated_immediate_full_frame_bytes"
)
ACTION_FRAME_SCREENSHOT = {
    "format": "provider-native",
    "show_cursor": None,
}
ACTION_FRAME_POINT = (512, 384)
ACTION_FRAME_ACTIONS = (
    {
        "type": "click",
        "x": ACTION_FRAME_POINT[0],
        "y": ACTION_FRAME_POINT[1],
        "button": "left",
    },
)
ACTION_FRAME_ACTION_PAYLOAD_SHA256 = hashlib.sha256(
    json.dumps(ACTION_FRAME_ACTIONS, separators=(",", ":"), sort_keys=True).encode("utf-8")
).hexdigest()


class ActionFrameDriver(Protocol):
    def action_to_immediate_frame(self, resource: Any) -> dict[str, Any]: ...


def run_action_to_immediate_frame_case(
    *,
    provider: str,
    driver: ActionFrameDriver,
    resource: Any,
    iterations: int,
    warmup_iterations: int,
    before_iteration: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Measure one ordered action batch followed by an immediate screenshot.

    ``driver.action_to_immediate_frame`` must use the provider's public SDK
    action and screenshot methods and return metadata only.  The helper keeps
    screenshot bytes out of artifacts while requiring the driver to validate
    that the bytes were received and decoded.
    """

    if iterations < 1:
        raise ValueError("iterations must be >= 1")
    if warmup_iterations < 0:
        raise ValueError("warmup_iterations must be >= 0")

    failures: list[dict[str, Any]] = []

    def operation() -> dict[str, Any]:
        observation = driver.action_to_immediate_frame(resource)
        return _validated_observation(observation)

    samples, observations = _measure_observed_case(
        name=ACTION_FRAME_CASE,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        operation=operation,
        failures=failures,
        before_iteration=before_iteration,
    )
    result = _case_result(ACTION_FRAME_CASE, iterations, samples, failures)
    result["failures"] = [_safe_failure(failure) for failure in result["failures"]]
    result.update(
        {
            "case_id": ACTION_FRAME_CASE_ID,
            "provider": provider,
            "path": _common_path(observations),
            "timer_boundary": ACTION_FRAME_TIMER_BOUNDARY,
            "action_semantics": ACTION_FRAME_SEMANTICS,
            "action_payload_sha256": ACTION_FRAME_ACTION_PAYLOAD_SHA256,
            "action_count": len(ACTION_FRAME_ACTIONS),
            "screenshot": _common_screenshot(observations),
            "request_shape": _common_request_shape(observations),
            "harness_retries": 0,
            "replacement_samples": 0,
            "cleanup": {"status": "managed_by_provider_lifecycle"},
            "last_result": (
                sanitize_provider_observation(observations[-1]) if observations else None
            ),
        }
    )
    return result


def _safe_failure(failure: Any) -> dict[str, Any]:
    if not isinstance(failure, Mapping):
        return {"case": ACTION_FRAME_CASE, "phase": "measure", "category": "unknown"}
    safe: dict[str, Any] = {
        "case": ACTION_FRAME_CASE,
        "phase": failure.get("phase", "measure"),
        "category": failure.get("type", "BenchmarkFailure"),
    }
    if isinstance(failure.get("iteration"), int):
        safe["iteration"] = failure["iteration"]
    return safe


def _validated_observation(observation: Any) -> dict[str, Any]:
    if not isinstance(observation, dict):
        raise ValueError("action-to-frame observation must be an object")
    screenshot = observation.get("screenshot")
    if not isinstance(screenshot, dict):
        raise ValueError("action-to-frame observation omitted screenshot metadata")
    _require_positive_int(screenshot.get("decoded_size_bytes"), "screenshot bytes")
    _require_positive_int(screenshot.get("width"), "screenshot width")
    _require_positive_int(screenshot.get("height"), "screenshot height")
    if not isinstance(screenshot.get("format"), str) or not screenshot["format"].strip():
        raise ValueError("action-to-frame screenshot format must be recorded")
    if screenshot.get("show_cursor") not in {True, False, None}:
        raise ValueError("action-to-frame cursor visibility must be boolean or unknown")
    actions = observation.get("actions")
    if not isinstance(actions, dict):
        raise ValueError("action-to-frame observation omitted action metadata")
    if actions.get("case_id") not in (None, ACTION_FRAME_CASE_ID):
        raise ValueError("action-to-frame action metadata has an unexpected case id")
    return observation


def _common_path(observations: list[dict[str, Any]]) -> str | None:
    values = {value.get("path") for value in observations if isinstance(value.get("path"), str)}
    return next(iter(values)) if len(values) == 1 else None


def _common_screenshot(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    values = []
    for observation in observations:
        screenshot = observation.get("screenshot")
        if not isinstance(screenshot, dict):
            continue
        values.append(
            {
                "format": screenshot.get("format"),
                "width": screenshot.get("width"),
                "height": screenshot.get("height"),
                "show_cursor": screenshot.get("show_cursor"),
            }
        )
    if not values or any(value != values[0] for value in values[1:]):
        return None
    return values[0]


def _common_request_shape(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    values: list[dict[str, Any]] = []
    for observation in observations:
        actions = observation.get("actions")
        if not isinstance(actions, Mapping):
            continue
        sdk_calls = actions.get("provider_sdk_call_count")
        transport_requests = actions.get("transport_request_count")
        batching = actions.get("batching")
        if not isinstance(sdk_calls, int) or not isinstance(transport_requests, int):
            continue
        values.append(
            {
                "sdk_calls": sdk_calls + 1,
                "transport_requests": transport_requests + 1,
                "batching": _normalize_batching(batching),
                "action_sdk_calls": sdk_calls,
                "action_transport_requests": transport_requests,
                "screenshot_sdk_calls": 1,
                "screenshot_transport_requests": 1,
            }
        )
    if not values or any(value != values[0] for value in values[1:]):
        return None
    return values[0]


def _normalize_batching(value: Any) -> str | None:
    if value in {"single_request", "single-request"}:
        return "single-request"
    if value in {"sequential_requests", "sequential-requests"}:
        return "sequential-requests"
    return value if isinstance(value, str) else None


def _require_positive_int(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")

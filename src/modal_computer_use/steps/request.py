from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ..models import ComputerAction, ScreenshotOptions, parse_action
from .models import ComputerStepResult


def build_step_payload(
    actions: list[ComputerAction | dict[str, Any]],
    *,
    continue_on_error: bool,
    screenshot_options: ScreenshotOptions | None,
    max_action_timeout_ms: int | None,
    call_id: str | None,
) -> dict[str, Any]:
    """Normalize the small, deliberate request surface of ``Computer.step``."""

    normalized = [parse_action(action) for action in actions]
    if screenshot_options is not None and screenshot_options.storage != "inline":
        raise ValueError("computer step screenshots must use inline storage")
    if screenshot_options is not None and screenshot_options.processing == "client":
        raise ValueError("computer step screenshots require daemon processing")
    payload: dict[str, Any] = {
        "actions": [action.model_dump(mode="json") for action in normalized],
        "continue_on_error": continue_on_error,
    }
    if call_id is not None:
        payload["call_id"] = call_id
    if screenshot_options is not None:
        payload["screenshot_options"] = screenshot_options.model_dump(mode="json")
    if max_action_timeout_ms is not None:
        payload["max_action_timeout_ms"] = max_action_timeout_ms
    return payload


def validate_step_screenshot_options(
    result: ComputerStepResult,
    payload: Mapping[str, Any],
) -> None:
    """Verify that the returned final frame matches the requested semantics."""

    from .codec import StepEnvelopeError

    options = payload.get("screenshot_options")
    expected_format = options.get("format", "png") if isinstance(options, Mapping) else "png"
    expected_cursor = (
        options.get("show_cursor", False) if isinstance(options, Mapping) else False
    )
    expected_scale = options.get("scale", 1.0) if isinstance(options, Mapping) else 1.0
    screenshot = result.screenshot
    coordinate_space = screenshot.coordinate_space
    source_width = (
        coordinate_space.source_region.width
        if coordinate_space.source_region is not None
        else coordinate_space.desktop_width
    )
    source_height = (
        coordinate_space.source_region.height
        if coordinate_space.source_region is not None
        else coordinate_space.desktop_height
    )
    expected_width = max(1, round(source_width * expected_scale))
    expected_height = max(1, round(source_height * expected_scale))
    if (
        screenshot.format != expected_format
        or screenshot.cursor_visible is not expected_cursor
        or not isinstance(expected_scale, int | float)
        or isinstance(expected_scale, bool)
        or not math.isclose(screenshot.width, expected_width)
        or not math.isclose(screenshot.height, expected_height)
    ):
        raise StepEnvelopeError()

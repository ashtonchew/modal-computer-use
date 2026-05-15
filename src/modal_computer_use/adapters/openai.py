from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from modal_computer_use.errors import ActionValidationError, UnsupportedActionError
from modal_computer_use.models import (
    ActionBatchResult,
    ActionResult,
    CoordinateSpace,
    Point,
    Screenshot,
)
from modal_computer_use.redaction import sanitize_payload

from .generic import ActionExecutor, PolicyHook
from .output import screenshot_data_url, screenshot_metadata
from .provenance import with_provider_provenance

_OPENAI_ACTIONS = {
    "click",
    "double_click",
    "scroll",
    "type",
    "keypress",
    "drag",
    "move",
    "wait",
    "screenshot",
}


class OpenAIAdapter:
    def __init__(
        self,
        computer: object,
        *,
        before_action: PolicyHook | None = None,
        coordinate_space: CoordinateSpace | None = None,
        allow_unknown: bool = False,
    ) -> None:
        self.executor = ActionExecutor(
            computer,
            before_action=before_action,
            coordinate_space=coordinate_space,
            allow_unknown=allow_unknown,
            source="openai-adapter",
        )
        self.allow_unknown = allow_unknown

    def normalize(self, action: dict[str, Any]) -> dict[str, Any]:
        kind = action.get("type") or action.get("action")
        if kind not in _OPENAI_ACTIONS:
            if self.allow_unknown:
                return with_provider_provenance(
                    {
                        "type": "wait",
                        "duration_ms": 0,
                    },
                    action,
                )
            raise UnsupportedActionError(f"unsupported OpenAI computer action: {kind}")
        _reject_unknown_fields(action)
        if kind == "click":
            x, y = _required_xy(action)
            return _with_common(
                {
                    "type": "click",
                    "x": x,
                    "y": y,
                    "button": action.get("button", "left"),
                    "modifiers": action.get("modifiers", []),
                },
                action,
            )
        if kind == "double_click":
            x, y = _required_xy(action)
            return _with_common({"type": "double_click", "x": x, "y": y}, action)
        if kind == "scroll":
            dx = int(action.get("scroll_x", action.get("dx", 0)) or 0)
            dy = int(action.get("scroll_y", action.get("dy", action.get("amount", 0))) or 0)
            if abs(dx) > abs(dy):
                direction = "right" if dx > 0 else "left"
                amount = abs(dx)
            else:
                direction = "down" if dy > 0 else "up"
                amount = abs(dy) or int(action.get("amount", 1))
            return _with_common(
                {
                    "type": "scroll",
                    "direction": direction,
                    "amount": amount,
                    "x": action.get("x"),
                    "y": action.get("y"),
                },
                action,
            )
        if kind == "type":
            if "text" not in action:
                raise ActionValidationError("OpenAI type action requires text")
            return _with_common({"type": "type", "text": action["text"]}, action)
        if kind == "keypress":
            keys = action.get("keys") or action.get("key")
            if not keys:
                raise ActionValidationError("OpenAI keypress action requires key or keys")
            if isinstance(keys, list) and len(keys) > 1:
                return _with_common({"type": "hotkey", "keys": keys}, action)
            if isinstance(keys, list):
                keys = keys[0]
            return _with_common(
                {
                    "type": "keypress",
                    "key": keys,
                    "modifiers": action.get("modifiers", []),
                },
                action,
            )
        if kind == "drag":
            if "path" in action:
                return _with_common(
                    {
                        "type": "drag",
                        "path": [
                            Point(x=point[0], y=point[1]).model_dump()
                            for point in action["path"]
                        ],
                        "button": action.get("button", "left"),
                        "modifiers": action.get("modifiers", []),
                    },
                    action,
                )
            if "end_x" not in action and "x" not in action:
                raise ActionValidationError("OpenAI drag action requires path or end coordinates")
            return _with_common(
                {
                    "type": "drag",
                    "start_x": action.get("start_x"),
                    "start_y": action.get("start_y"),
                    "end_x": action.get("end_x", action.get("x")),
                    "end_y": action.get("end_y", action.get("y")),
                    "button": action.get("button", "left"),
                    "modifiers": action.get("modifiers", []),
                },
                action,
            )
        if kind == "move":
            x, y = _required_xy(action)
            return _with_common({"type": "move", "x": x, "y": y}, action)
        if kind == "wait":
            return _with_common(
                {
                    "type": "wait",
                    "duration_ms": int(action.get("duration_ms", action.get("ms", 1000))),
                },
                action,
            )
        if kind == "screenshot":
            return _with_common({"type": "screenshot"}, action)
        raise UnsupportedActionError(f"unsupported OpenAI computer action: {kind}")

    def apply(self, action: dict[str, Any]) -> ActionResult:
        return self.executor.apply(self.normalize(action))

    def apply_many(
        self,
        actions: Iterable[dict[str, Any]],
        *,
        continue_on_error: bool = False,
        screenshot_after: bool = False,
        max_action_timeout_ms: int | None = None,
    ) -> ActionBatchResult:
        return self.executor.apply_many(
            [self.normalize(action) for action in actions],
            continue_on_error=continue_on_error,
            screenshot_after=screenshot_after,
            max_action_timeout_ms=max_action_timeout_ms,
        )


def openai_computer_call_output(
    screenshot: Screenshot,
    *,
    call_id: str,
    current_url: str | None = None,
    acknowledged_safety_checks: list[dict[str, Any]] | None = None,
    detail: str = "original",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "computer_call_output",
        "call_id": call_id,
        "output": {
            "type": "computer_screenshot",
            "image_url": screenshot_data_url(screenshot),
            "detail": detail,
        },
    }
    if current_url is not None:
        payload["current_url"] = current_url
    if acknowledged_safety_checks is not None:
        payload["acknowledged_safety_checks"] = acknowledged_safety_checks
    return payload


def openai_screenshot_metadata(screenshot: Screenshot) -> dict[str, Any]:
    return screenshot_metadata(screenshot)


def _required_xy(action: dict[str, Any]) -> tuple[int, int]:
    if "x" not in action or "y" not in action:
        kind = action.get("type") or action.get("action")
        raise ActionValidationError(f"OpenAI {kind} action requires x and y")
    return int(action["x"]), int(action["y"])


def _with_common(payload: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    for key in ("metadata", "call_id", "sequence", "timeout_ms"):
        if key in action:
            payload[key] = sanitize_payload(action[key]) if key == "metadata" else action[key]
    return with_provider_provenance(payload, action)


def _reject_unknown_fields(action: dict[str, Any]) -> None:
    allowed = {
        "action",
        "type",
        "x",
        "y",
        "button",
        "modifiers",
        "scroll_x",
        "scroll_y",
        "dx",
        "dy",
        "amount",
        "text",
        "keys",
        "key",
        "path",
        "start_x",
        "start_y",
        "end_x",
        "end_y",
        "duration_ms",
        "ms",
        "metadata",
        "call_id",
        "sequence",
        "timeout_ms",
    }
    unknown = sorted(set(action) - allowed)
    if unknown:
        raise ActionValidationError(f"OpenAI action contains unknown fields: {', '.join(unknown)}")

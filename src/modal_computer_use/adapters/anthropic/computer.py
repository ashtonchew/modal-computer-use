from __future__ import annotations

import json
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

from ..generic import ActionExecutor, PolicyHook
from ..output import action_result_summary, screenshot_media_type, screenshot_metadata
from ..provenance import with_provider_provenance
from .versions import AnthropicToolVersion, get_tool_version

_ANTHROPIC_ACTIONS = {
    "mouse_move",
    "left_click_drag",
    "key",
    "type",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "left_mouse_down",
    "mouse_down",
    "left_mouse_up",
    "mouse_up",
    "scroll",
    "hold_key",
    "wait",
    "screenshot",
    "zoom",
    "cursor_position",
}


class AnthropicAdapter:
    def __init__(
        self,
        computer: object,
        *,
        tool_version: str = "computer_20241022",
        beta_header: str | None = None,
        enable_zoom: bool | None = None,
        before_action: PolicyHook | None = None,
        coordinate_space: CoordinateSpace | None = None,
        allow_unknown: bool = False,
    ) -> None:
        self.computer = computer
        self.version: AnthropicToolVersion = get_tool_version(tool_version)
        self.beta_header = beta_header or self.version.beta_header
        self.enable_zoom = self.version.supports_zoom if enable_zoom is None else enable_zoom
        self.allow_unknown = allow_unknown
        self.executor = ActionExecutor(
            computer,
            before_action=before_action,
            coordinate_space=coordinate_space,
            allow_unknown=allow_unknown,
            source="anthropic-adapter",
        )

    def normalize(self, action: dict[str, Any]) -> dict[str, Any]:
        name = action.get("action") or action.get("type")
        if name not in _ANTHROPIC_ACTIONS:
            if self.allow_unknown:
                return with_provider_provenance({"type": "wait", "duration_ms": 0}, action)
            raise UnsupportedActionError(f"unsupported Anthropic computer action: {name}")
        _reject_unknown_fields(action)
        coord = _coord(action.get("coordinate"))
        if name == "mouse_move":
            if coord is None:
                raise UnsupportedActionError("mouse_move requires coordinate")
            return _with_common({"type": "move", "x": coord.x, "y": coord.y}, action)
        if name == "left_click_drag":
            start = _coord(action.get("start_coordinate"))
            if coord is None:
                raise UnsupportedActionError("left_click_drag requires destination coordinate")
            if start is None:
                return _with_common({"type": "drag", "end_x": coord.x, "end_y": coord.y}, action)
            return _with_common(
                {
                    "type": "drag",
                    "start_x": start.x,
                    "start_y": start.y,
                    "end_x": coord.x,
                    "end_y": coord.y,
                },
                action,
            )
        if name == "key":
            key = action.get("text") or action.get("key")
            if not key:
                raise UnsupportedActionError("key action requires text/key")
            if "+" in key or " " in key:
                return _with_common(
                    {"type": "hotkey", "keys": key.replace("+", " ").split()},
                    action,
                )
            return _with_common({"type": "keypress", "key": key}, action)
        if name == "type":
            if "text" not in action:
                raise ActionValidationError("type action requires text")
            return _with_common({"type": "type", "text": action.get("text", "")}, action)
        if name in {"left_click", "right_click", "middle_click"}:
            button = name.split("_")[0]
            return _with_common(_click(button, coord), action)
        if name == "double_click":
            return _with_common(_click("left", coord, kind="double_click"), action)
        if name == "triple_click":
            self._require_enhanced(name)
            return _with_common(_click("left", coord, kind="triple_click"), action)
        if name in {"left_mouse_down", "mouse_down"}:
            self._require_enhanced(name)
            return _with_common(_button("mouse_down", coord), action)
        if name in {"left_mouse_up", "mouse_up"}:
            self._require_enhanced(name)
            return _with_common(_button("mouse_up", coord), action)
        if name == "scroll":
            self._require_enhanced(name)
            return _with_common(
                {
                    "type": "scroll",
                    "direction": action.get("direction", "down"),
                    "amount": int(action.get("amount", 1)),
                    "x": coord.x if coord else None,
                    "y": coord.y if coord else None,
                },
                action,
            )
        if name == "hold_key":
            self._require_enhanced(name)
            key = action.get("key") or action.get("text")
            if not key:
                raise ActionValidationError("hold_key action requires key or text")
            payload: dict[str, Any] = {
                "type": "hold_key",
                "key": key,
                "duration_ms": action.get("duration_ms"),
            }
            if "actions" in action:
                nested_actions = action["actions"]
                if not isinstance(nested_actions, list):
                    raise ActionValidationError("hold_key actions must be a list")
                payload["actions"] = [self.normalize(item) for item in nested_actions]
            return _with_common(
                payload,
                action,
            )
        if name == "wait":
            self._require_enhanced(name)
            return _with_common(
                {"type": "wait", "duration_ms": int(action.get("duration_ms", 1000))},
                action,
            )
        if name == "screenshot":
            return _with_common({"type": "screenshot"}, action)
        if name == "zoom":
            if not self.enable_zoom or not self.version.supports_zoom:
                raise UnsupportedActionError("zoom is not enabled for this Anthropic tool version")
            if "region" not in action:
                raise ActionValidationError("zoom action requires region")
            return _with_common(
                {"type": "zoom", "region": action["region"], "scale": action.get("scale", 2.0)},
                action,
            )
        if name == "cursor_position":
            return _with_common({"type": "cursor_position"}, action)
        raise UnsupportedActionError(f"unsupported Anthropic computer action: {name}")

    def _require_enhanced(self, action_name: str) -> None:
        if not self.version.supports_enhanced_actions:
            raise UnsupportedActionError(
                f"{action_name} is not supported by Anthropic tool version {self.version.name}"
            )

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


def anthropic_screenshot_content(screenshot: Screenshot) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": screenshot_media_type(screenshot),
            "data": screenshot.to_base64(),
        },
    }


def anthropic_tool_result(
    *,
    tool_use_id: str,
    result: Screenshot | ActionResult,
) -> dict[str, Any]:
    if isinstance(result, Screenshot):
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": [anthropic_screenshot_content(result)],
        }
    payload: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": [{"type": "text", "text": _json_summary(action_result_summary(result))}],
    }
    if not result.ok:
        payload["is_error"] = True
    return payload


def anthropic_screenshot_metadata(screenshot: Screenshot) -> dict[str, Any]:
    return screenshot_metadata(screenshot)


def _json_summary(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _coord(value: object) -> Point | None:
    if value is None:
        return None
    if isinstance(value, dict):
        if "x" not in value or "y" not in value:
            raise ActionValidationError("coordinate object requires x and y")
        return Point(x=int(value["x"]), y=int(value["y"]))
    if isinstance(value, list | tuple) and len(value) == 2:
        return Point(x=int(value[0]), y=int(value[1]))
    raise UnsupportedActionError("coordinate must be [x, y] or {x, y}")


def _click(button: str, coord: Point | None, *, kind: str = "click") -> dict[str, Any]:
    payload: dict[str, Any] = {"type": kind, "button": button}
    if coord:
        payload.update({"x": coord.x, "y": coord.y})
    return payload


def _button(kind: str, coord: Point | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": kind, "button": "left"}
    if coord:
        payload.update({"x": coord.x, "y": coord.y})
    return payload


def _with_common(payload: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    for key in ("metadata", "call_id", "sequence", "timeout_ms"):
        if key in action:
            payload[key] = sanitize_payload(action[key]) if key == "metadata" else action[key]
    return with_provider_provenance(payload, action)


def _reject_unknown_fields(action: dict[str, Any]) -> None:
    allowed = {
        "action",
        "type",
        "coordinate",
        "start_coordinate",
        "text",
        "key",
        "direction",
        "amount",
        "duration_ms",
        "actions",
        "region",
        "scale",
        "metadata",
        "call_id",
        "sequence",
        "timeout_ms",
    }
    unknown = sorted(set(action) - allowed)
    if unknown:
        raise ActionValidationError(
            f"Anthropic action contains unknown fields: {', '.join(unknown)}"
        )

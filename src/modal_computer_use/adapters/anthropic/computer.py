from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from modal_computer_use.errors import UnsupportedActionError
from modal_computer_use.models import ActionBatchResult, ActionResult, CoordinateSpace, Point

from ..generic import ActionExecutor, PolicyHook
from .versions import AnthropicToolVersion, get_tool_version


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
        )

    def normalize(self, action: dict[str, Any]) -> dict[str, Any]:
        name = action.get("action") or action.get("type")
        coord = _coord(action.get("coordinate"))
        if name == "mouse_move":
            if coord is None:
                raise UnsupportedActionError("mouse_move requires coordinate")
            return {"type": "move", "x": coord.x, "y": coord.y}
        if name == "left_click_drag":
            start = _coord(action.get("start_coordinate"))
            if coord is None:
                raise UnsupportedActionError("left_click_drag requires destination coordinate")
            if start is None:
                return {"type": "drag", "end_x": coord.x, "end_y": coord.y}
            return {
                "type": "drag",
                "start_x": start.x,
                "start_y": start.y,
                "end_x": coord.x,
                "end_y": coord.y,
            }
        if name == "key":
            key = action.get("text") or action.get("key")
            if not key:
                raise UnsupportedActionError("key action requires text/key")
            if "+" in key or " " in key:
                return {"type": "hotkey", "keys": key.replace("+", " ").split()}
            return {"type": "keypress", "key": key}
        if name == "type":
            return {"type": "type", "text": action.get("text", "")}
        if name in {"left_click", "right_click", "middle_click"}:
            button = name.split("_")[0]
            return _click(button, coord)
        if name == "double_click":
            return _click("left", coord, kind="double_click")
        if name == "triple_click":
            return _click("left", coord, kind="triple_click")
        if name in {"left_mouse_down", "mouse_down"}:
            return _button("mouse_down", coord)
        if name in {"left_mouse_up", "mouse_up"}:
            return _button("mouse_up", coord)
        if name == "scroll":
            return {
                "type": "scroll",
                "direction": action.get("direction", "down"),
                "amount": int(action.get("amount", 1)),
                "x": coord.x if coord else None,
                "y": coord.y if coord else None,
            }
        if name == "hold_key":
            return {
                "type": "hold_key",
                "key": action.get("key") or action.get("text"),
                "duration_ms": action.get("duration_ms"),
            }
        if name == "wait":
            return {"type": "wait", "duration_ms": int(action.get("duration_ms", 1000))}
        if name == "screenshot":
            return {"type": "screenshot"}
        if name == "zoom":
            if not self.enable_zoom:
                raise UnsupportedActionError("zoom is not enabled for this Anthropic tool version")
            return {"type": "zoom", "region": action["region"], "scale": action.get("scale", 2.0)}
        if name == "cursor_position":
            return {"type": "cursor_position"}
        if self.allow_unknown:
            return {
                "type": "wait",
                "duration_ms": 0,
                "metadata": {"unknown_provider_action": action},
            }
        raise UnsupportedActionError(f"unsupported Anthropic computer action: {name}")

    def apply(self, action: dict[str, Any]) -> ActionResult:
        return self.executor.apply(self.normalize(action))

    def apply_many(self, actions: Iterable[dict[str, Any]]) -> ActionBatchResult:
        return self.executor.apply_many([self.normalize(action) for action in actions])


def _coord(value: object) -> Point | None:
    if value is None:
        return None
    if isinstance(value, dict):
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

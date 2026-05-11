from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from modal_computer_use.errors import UnsupportedActionError
from modal_computer_use.models import (
    ActionBatchResult,
    ActionResult,
    CoordinateSpace,
    Point,
)

from .generic import ActionExecutor, PolicyHook


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
        )
        self.allow_unknown = allow_unknown

    def normalize(self, action: dict[str, Any]) -> dict[str, Any]:
        kind = action.get("type") or action.get("action")
        if kind == "click":
            return {
                "type": "click",
                "x": action.get("x"),
                "y": action.get("y"),
                "button": action.get("button", "left"),
                "modifiers": action.get("modifiers", []),
            }
        if kind == "double_click":
            return {"type": "double_click", "x": action.get("x"), "y": action.get("y")}
        if kind == "scroll":
            dx = int(action.get("scroll_x", action.get("dx", 0)) or 0)
            dy = int(action.get("scroll_y", action.get("dy", action.get("amount", 0))) or 0)
            if abs(dx) > abs(dy):
                direction = "right" if dx > 0 else "left"
                amount = abs(dx)
            else:
                direction = "down" if dy > 0 else "up"
                amount = abs(dy) or int(action.get("amount", 1))
            return {
                "type": "scroll",
                "direction": direction,
                "amount": amount,
                "x": action.get("x"),
                "y": action.get("y"),
            }
        if kind == "type":
            return {"type": "type", "text": action.get("text", "")}
        if kind == "keypress":
            keys = action.get("keys") or action.get("key")
            if isinstance(keys, list) and len(keys) > 1:
                return {"type": "hotkey", "keys": keys}
            if isinstance(keys, list):
                keys = keys[0]
            return {"type": "keypress", "key": keys}
        if kind == "drag":
            if "path" in action:
                return {
                    "type": "drag",
                    "path": [
                        Point(x=point[0], y=point[1]).model_dump() for point in action["path"]
                    ],
                }
            return {
                "type": "drag",
                "start_x": action.get("start_x"),
                "start_y": action.get("start_y"),
                "end_x": action.get("end_x", action.get("x")),
                "end_y": action.get("end_y", action.get("y")),
            }
        if kind == "move":
            return {"type": "move", "x": action["x"], "y": action["y"]}
        if kind == "wait":
            return {
                "type": "wait",
                "duration_ms": int(action.get("duration_ms", action.get("ms", 1000))),
            }
        if kind == "screenshot":
            return {"type": "screenshot"}
        if self.allow_unknown:
            return {
                "type": "wait",
                "duration_ms": 0,
                "metadata": {"unknown_provider_action": action},
            }
        raise UnsupportedActionError(f"unsupported OpenAI computer action: {kind}")

    def apply(self, action: dict[str, Any]) -> ActionResult:
        return self.executor.apply(self.normalize(action))

    def apply_many(self, actions: Iterable[dict[str, Any]]) -> ActionBatchResult:
        return self.executor.apply_many([self.normalize(action) for action in actions])

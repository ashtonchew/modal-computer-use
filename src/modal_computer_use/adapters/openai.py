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
_OPENAI_BUTTONS = {
    "left": "left",
    "right": "right",
    "wheel": "middle",
    "back": "back",
    "forward": "forward",
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
                    "button": _button(action.get("button", "left")),
                    "modifiers": _modifier_keys(action),
                },
                action,
            )
        if kind == "double_click":
            x, y = _required_xy(action)
            return _with_common(
                {
                    "type": "double_click",
                    "x": x,
                    "y": y,
                    "modifiers": _modifier_keys(action),
                },
                action,
            )
        if kind == "scroll":
            normalized = _scroll_actions(action)
            if len(normalized) != 1:
                raise ActionValidationError(
                    "OpenAI two-axis scroll actions require apply_many()"
                )
            return normalized[0]
        if kind == "type":
            if "text" not in action:
                raise ActionValidationError("OpenAI type action requires text")
            return _with_common({"type": "type", "text": action["text"]}, action)
        if kind == "keypress":
            keys = action.get("keys") or action.get("key")
            if not keys:
                raise ActionValidationError("OpenAI keypress action requires key or keys")
            if isinstance(keys, list) and len(keys) > 1:
                raise ActionValidationError(
                    "OpenAI multi-key keypress actions require apply_many()"
                )
            if isinstance(keys, list):
                keys = keys[0]
            return _with_common(
                {
                    "type": "keypress",
                    "key": keys,
                },
                action,
            )
        if kind == "drag":
            if "path" in action:
                return _with_common(
                    {
                        "type": "drag",
                        "path": [_point(point).model_dump() for point in action["path"]],
                        "button": _button(action.get("button", "left")),
                        "modifiers": _modifier_keys(action),
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
                    "button": _button(action.get("button", "left")),
                    "modifiers": _modifier_keys(action),
                },
                action,
            )
        if kind == "move":
            x, y = _required_xy(action)
            return _with_modifiers(
                _with_common({"type": "move", "x": x, "y": y}, action),
                _modifier_keys(action),
            )
        if kind == "wait":
            return _with_common(
                {
                    "type": "wait",
                    "duration_ms": int(action.get("duration_ms", action.get("ms", 2000))),
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
        normalized: list[dict[str, Any]] = []
        for action in actions:
            kind = action.get("type") or action.get("action")
            if kind == "keypress" and isinstance(action.get("keys"), list):
                _reject_unknown_fields(action)
                keys = action["keys"]
                if not keys:
                    raise ActionValidationError("OpenAI keypress action requires keys")
                normalized.extend(
                    _with_common({"type": "keypress", "key": key}, action)
                    for key in keys
                )
            elif kind == "scroll":
                _reject_unknown_fields(action)
                normalized.extend(_scroll_actions(action))
            else:
                normalized.append(self.normalize(action))
        return self.executor.apply_many(
            normalized,
            continue_on_error=continue_on_error,
            screenshot_after=screenshot_after,
            max_action_timeout_ms=max_action_timeout_ms,
        )


def openai_computer_call_output(
    screenshot: Screenshot,
    *,
    call_id: str,
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
    return payload


def openai_screenshot_metadata(screenshot: Screenshot) -> dict[str, Any]:
    return screenshot_metadata(screenshot)


def _required_xy(action: dict[str, Any]) -> tuple[int, int]:
    if "x" not in action or "y" not in action:
        kind = action.get("type") or action.get("action")
        raise ActionValidationError(f"OpenAI {kind} action requires x and y")
    return int(action["x"]), int(action["y"])


def _button(value: object) -> str:
    try:
        return _OPENAI_BUTTONS[str(value)]
    except KeyError as exc:
        raise ActionValidationError(f"unsupported OpenAI mouse button: {value}") from exc


def _modifier_keys(action: dict[str, Any]) -> list[str]:
    keys = action.get("keys", action.get("modifiers", []))
    if keys is None:
        return []
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise ActionValidationError("OpenAI action keys must be a list of strings")
    return keys


def _with_modifiers(payload: dict[str, Any], modifiers: list[str]) -> dict[str, Any]:
    wrapped = payload
    for key in reversed(modifiers):
        wrapped = {"type": "hold_key", "key": key, "actions": [wrapped]}
    return wrapped


def _point(value: object) -> Point:
    if isinstance(value, dict) and "x" in value and "y" in value:
        return Point(x=int(value["x"]), y=int(value["y"]))
    if isinstance(value, list | tuple) and len(value) >= 2:
        return Point(x=int(value[0]), y=int(value[1]))
    raise ActionValidationError(
        "OpenAI drag path entries must be [x, y] pairs or {x, y} objects"
    )


def _scroll_actions(action: dict[str, Any]) -> list[dict[str, Any]]:
    dx = int(action.get("scroll_x", action.get("dx", 0)) or 0)
    dy = int(action.get("scroll_y", action.get("dy", 0)) or 0)
    if not dx and not dy and "amount" in action:
        dy = int(action["amount"])
    normalized: list[dict[str, Any]] = []
    for delta, negative, positive in (
        (dy, "up", "down"),
        (dx, "left", "right"),
    ):
        if not delta:
            continue
        payload = _with_common(
            {
                "type": "scroll",
                "direction": negative if delta < 0 else positive,
                "amount": max(1, abs(round(delta / 100))),
                "x": action.get("x"),
                "y": action.get("y"),
            },
            action,
        )
        normalized.append(_with_modifiers(payload, _modifier_keys(action)))
    if not normalized:
        raise ActionValidationError("OpenAI scroll action requires a non-zero delta")
    return normalized


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

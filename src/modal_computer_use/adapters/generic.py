from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from modal_computer_use.errors import UnsupportedActionError
from modal_computer_use.models import (
    ActionBatchResult,
    ActionDecision,
    ActionResult,
    ComputerAction,
    CoordinateSpace,
    Point,
    Region,
    parse_action,
)

PolicyHook = Callable[[ComputerAction, dict[str, Any]], ActionDecision | None]
AfterHook = Callable[[ComputerAction, ActionResult | ActionBatchResult], None]


class ActionExecutor:
    def __init__(
        self,
        computer: object,
        *,
        before_action: PolicyHook | None = None,
        after_action: AfterHook | None = None,
        coordinate_space: CoordinateSpace | None = None,
        allow_unknown: bool = False,
        source: str = "generic-adapter",
    ) -> None:
        self.computer = computer
        self.before_action = before_action
        self.after_action = after_action
        self.coordinate_space = coordinate_space
        self.allow_unknown = allow_unknown
        self.source = source

    def apply(self, action: ComputerAction | dict[str, Any]) -> ActionResult:
        normalized = self._transform(parse_action(action))
        self._policy_tree(normalized)
        result = self.computer.actions.apply(normalized)
        if self.after_action:
            self.after_action(normalized, result)
        return result

    def apply_many(
        self,
        actions: Iterable[ComputerAction | dict[str, Any]],
        *,
        continue_on_error: bool = False,
        screenshot_after: bool = False,
    ) -> ActionBatchResult:
        normalized = [self._transform(parse_action(action)) for action in actions]
        for action in normalized:
            self._policy_tree(action)
        result = self.computer.actions.run(
            normalized,
            continue_on_error=continue_on_error,
            screenshot_after=screenshot_after,
            source=self.source,
        )
        if self.after_action:
            for action in normalized:
                self.after_action(action, result)
        return result

    def _policy_tree(self, action: ComputerAction) -> None:
        self._policy(action)
        if getattr(action, "type", None) != "hold_key":
            return
        for nested in getattr(action, "actions", None) or []:
            self._policy_tree(parse_action(nested))

    def _policy(self, action: ComputerAction) -> None:
        if not self.before_action:
            return
        decision = self.before_action(
            action,
            {
                "source": self.source,
                "coordinate_space": self.coordinate_space,
                "coordinates_transformed": self.coordinate_space is not None,
            },
        )
        if decision and decision.decision != "allow":
            raise UnsupportedActionError(decision.reason or f"action denied: {action.type}")

    def _transform(self, action: ComputerAction) -> ComputerAction:
        if self.coordinate_space is None:
            return action
        updates: dict[str, Any] = {}
        if hasattr(action, "x") and action.x is not None:
            point = self.coordinate_space.to_desktop(
                Point(x=action.x, y=action.y)
            )
            updates.update({"x": point.x, "y": point.y})
        if hasattr(action, "start_x") and action.start_x is not None:
            point = self.coordinate_space.to_desktop(
                Point(x=action.start_x, y=action.start_y)
            )
            updates.update({"start_x": point.x, "start_y": point.y})
        if hasattr(action, "end_x") and action.end_x is not None:
            point = self.coordinate_space.to_desktop(
                Point(x=action.end_x, y=action.end_y)
            )
            updates.update({"end_x": point.x, "end_y": point.y})
        path = getattr(action, "path", None)
        if path:
            updates["path"] = [self.coordinate_space.to_desktop(point) for point in path]
        region = getattr(action, "region", None)
        if isinstance(region, Region):
            top_left = self.coordinate_space.to_desktop(Point(x=region.x, y=region.y))
            bottom_right = self.coordinate_space.to_desktop(
                Point(x=region.right, y=region.bottom)
            )
            updates["region"] = Region(
                x=top_left.x,
                y=top_left.y,
                width=max(1, bottom_right.x - top_left.x),
                height=max(1, bottom_right.y - top_left.y),
            )
        nested_actions = getattr(action, "actions", None)
        if getattr(action, "type", None) == "hold_key" and nested_actions:
            updates["actions"] = [
                self._transform(parse_action(item)).model_dump(mode="json")
                for item in nested_actions
            ]
        if not updates:
            return action
        return action.model_copy(update=updates)
